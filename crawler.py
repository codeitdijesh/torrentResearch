from __future__ import annotations

import os
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from database import IndexRecord, LazarusDatabase
from metadata import AI_CUTOFF_TIMESTAMP, TorrentMetadata, parse_magnet_link


try:
    import libtorrent as lt
except ImportError:  # pragma: no cover - depends on host environment.
    lt = None  # type: ignore[assignment]


class LibtorrentMissingError(RuntimeError):
    pass


@dataclass(slots=True)
class CrawlerConfig:
    db_path: Path
    duration_seconds: int = 300
    max_infohashes: int = 1_000
    metadata_timeout: int = 45
    request_interval: float = 1.0
    temp_path: Path | None = None


class SwarmCrawler:
    def __init__(self, config: CrawlerConfig) -> None:
        if lt is None:
            raise LibtorrentMissingError(
                "libtorrent Python bindings are not installed. Install them for your "
                "platform, then run the crawler again."
            )
        self.config = config
        self.temp_path = Path(config.temp_path or tempfile.mkdtemp(prefix="lazarus-indexer-"))
        self.session = self._new_session()
        self.pending_nodes: deque[object] = deque()
        self.pending_infohashes: deque[str] = deque()
        self.active: dict[str, object] = {}
        self.attempt_started: dict[str, float] = {}
        self.seen_infohashes: set[str] = set()
        self.local_node_ids: set[str] = set()

    def crawl(self) -> dict[str, int]:
        deadline = time.monotonic() + self.config.duration_seconds
        sampled = 0
        with LazarusDatabase(self.config.db_path) as db:
            db.initialize()
            self._request_live_nodes()
            while time.monotonic() < deadline and sampled < self.config.max_infohashes:
                self._drain_alerts(db)
                self._expire_metadata_attempts(db)
                self._sample_next_node()
                sampled += self._start_metadata_attempts(db)
                time.sleep(self.config.request_interval)
            self._drain_alerts(db)
            return db.summary()

    def ingest_magnets(self, magnets: Iterable[str]) -> int:
        inserted = 0
        with LazarusDatabase(self.config.db_path) as db:
            db.initialize()
            for magnet in magnets:
                metadata = parse_magnet_link(magnet)
                db.upsert_record(metadata.to_record())
                self.pending_infohashes.append(metadata.infohash)
                inserted += 1
            deadline = time.monotonic() + self.config.metadata_timeout
            while time.monotonic() < deadline and self.pending_infohashes:
                self._start_metadata_attempts(db)
                self._drain_alerts(db)
                self._expire_metadata_attempts(db)
                time.sleep(self.config.request_interval)
        return inserted

    def _new_session(self) -> object:
        settings = {
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": False,
            "enable_natpmp": False,
            "listen_interfaces": "0.0.0.0:6881,[::]:6881",
            "alert_mask": 0x7FFFFFFF,
        }
        try:
            session = lt.session(settings)  # type: ignore[union-attr]
        except TypeError:
            session = lt.session()  # type: ignore[union-attr]
            session.apply_settings(settings)

        if hasattr(session, "start_dht"):
            session.start_dht()
        if hasattr(session, "start_lsd"):
            session.start_lsd()
        for host, port in (
            ("router.bittorrent.com", 6881),
            ("router.utorrent.com", 6881),
            ("dht.transmissionbt.com", 6881),
        ):
            if hasattr(session, "add_dht_router"):
                session.add_dht_router(host, port)
        return session

    def _request_live_nodes(self) -> None:
        if not self.local_node_ids:
            if hasattr(self.session, "post_dht_stats"):
                self.session.post_dht_stats()
            return
        if hasattr(self.session, "dht_live_nodes"):
            for node_id in list(self.local_node_ids):
                self.session.dht_live_nodes(lt.sha1_hash(bytes.fromhex(node_id)))

    def _sample_next_node(self) -> None:
        if not self.pending_nodes:
            self._request_live_nodes()
            return
        node = self.pending_nodes.popleft()
        endpoint = _node_endpoint(node)
        if endpoint is None:
            return
        if hasattr(self.session, "dht_sample_infohashes"):
            self.session.dht_sample_infohashes(endpoint, self._sha1_hash())

    def _start_metadata_attempts(self, db: LazarusDatabase) -> int:
        started = 0
        while self.pending_infohashes and len(self.active) < 32:
            infohash = self.pending_infohashes.popleft().lower()
            if infohash in self.active or infohash in self.seen_infohashes:
                continue
            self.seen_infohashes.add(infohash)
            db.upsert_record(
                IndexRecord(
                    infohash=infohash,
                    source="dht",
                    metadata_status="pending",
                )
            )
            handle = self._add_metadata_probe(infohash)
            if handle is not None:
                self.active[infohash] = handle
                self.attempt_started[infohash] = time.monotonic()
                started += 1
        return started

    def _add_metadata_probe(self, infohash: str) -> object | None:
        magnet = f"magnet:?xt=urn:btih:{infohash}&dn={infohash}"
        try:
            if hasattr(lt, "add_magnet_uri"):
                handle = lt.add_magnet_uri(  # type: ignore[union-attr]
                    self.session,
                    magnet,
                    {"save_path": str(self.temp_path), "flags": self._torrent_flags()},
                )
            else:
                add_params = lt.parse_magnet_uri(magnet)  # type: ignore[union-attr]
                add_params.save_path = str(self.temp_path)
                add_params.flags = self._torrent_flags()
                handle = self.session.add_torrent(add_params)
            self._set_metadata_only(handle)
            return handle
        except Exception:
            return None

    def _torrent_flags(self) -> object:
        flags_api = getattr(lt, "torrent_flags", None)
        if flags_api is None:
            return 0
        default_flags = getattr(flags_api, "default_flags", 0)
        upload_mode = getattr(flags_api, "upload_mode", 0)
        duplicate_is_error = getattr(flags_api, "duplicate_is_error", 0)
        return default_flags | upload_mode | duplicate_is_error

    @staticmethod
    def _set_metadata_only(handle: object) -> None:
        if hasattr(handle, "set_upload_mode"):
            handle.set_upload_mode(True)
        if hasattr(handle, "set_sequential_download"):
            handle.set_sequential_download(False)

    def _drain_alerts(self, db: LazarusDatabase) -> None:
        alerts = self.session.pop_alerts() if hasattr(self.session, "pop_alerts") else []
        for alert in alerts:
            alert_name = _alert_name(alert)
            if (
                "dht_stats" in alert_name
                or "dht_live_nodes" in alert_name
                or "dht_sample_infohashes" in alert_name
            ):
                self._capture_dht_alert(alert)
            if "metadata_received" in alert_name:
                self._capture_metadata(alert, db)
            if "tracker_reply" in alert_name or "scrape_reply" in alert_name:
                self._capture_swarm_status(alert, db)
            if "metadata_failed" in alert_name or "torrent_error" in alert_name:
                self._capture_error(alert, db)

    def _capture_dht_alert(self, alert: object) -> None:
        self._capture_local_node_id(alert)
        for node in _alert_values(alert, "nodes"):
            self.pending_nodes.append(node)
        for sample in _alert_values(alert, "samples"):
            infohash = _sha1_to_hex(sample)
            if infohash not in self.seen_infohashes:
                self.pending_infohashes.append(infohash)

    def _capture_local_node_id(self, alert: object) -> None:
        message = _alert_message(alert)
        match = re.search(r"\(([0-9a-fA-F]{40})\)", message)
        if match and match.group(1) != "0" * 40:
            self.local_node_ids.add(match.group(1).lower())

    def _capture_metadata(self, alert: object, db: LazarusDatabase) -> None:
        handle = getattr(alert, "handle", None)
        if handle is None:
            return
        try:
            torrent_info = handle.get_torrent_info()
            infohash = _handle_infohash(handle)
            name = torrent_info.name()
            size = int(torrent_info.total_size())
            status = handle.status()
            seeds = int(getattr(status, "num_seeds", 0) or 0)
            peers = int(getattr(status, "num_peers", 0) or 0)
            leechers = max(peers - seeds, 0)
            db.upsert_record(
                IndexRecord(
                    infohash=infohash,
                    name=name,
                    size_bytes=size,
                    creation_date=None,
                    leechers=leechers,
                    seeds=seeds,
                    peers=peers,
                    is_recoverable=seeds > 0,
                    source="dht",
                    metadata_status="metadata_without_creation_date",
                )
            )
            self.active.pop(infohash, None)
            self.attempt_started.pop(infohash, None)
            self._remove_torrent(handle)
        except Exception as exc:
            self._record_handle_error(handle, db, str(exc))

    def _capture_swarm_status(self, alert: object, db: LazarusDatabase) -> None:
        handle = getattr(alert, "handle", None)
        if handle is None:
            return
        try:
            status = handle.status()
            seeds = int(getattr(status, "num_seeds", 0) or 0)
            peers = int(getattr(status, "num_peers", 0) or 0)
            leechers = max(peers - seeds, 0)
            db.upsert_record(
                IndexRecord(
                    infohash=_handle_infohash(handle),
                    leechers=leechers,
                    seeds=seeds,
                    peers=peers,
                    is_recoverable=seeds > 0,
                    metadata_status="pending",
                )
            )
        except Exception:
            return

    def _capture_error(self, alert: object, db: LazarusDatabase) -> None:
        handle = getattr(alert, "handle", None)
        if handle is None:
            return
        self._record_handle_error(handle, db, _alert_message(alert))

    def _record_handle_error(self, handle: object, db: LazarusDatabase, error: str) -> None:
        try:
            infohash = _handle_infohash(handle)
        except Exception:
            return
        db.upsert_record(
            IndexRecord(
                infohash=infohash,
                metadata_status="failed",
                error=error,
            )
        )
        self.active.pop(infohash, None)
        self.attempt_started.pop(infohash, None)
        self._remove_torrent(handle)

    def _expire_metadata_attempts(self, db: LazarusDatabase) -> None:
        now = time.monotonic()
        expired = [
            infohash
            for infohash, started in self.attempt_started.items()
            if now - started >= self.config.metadata_timeout
        ]
        for infohash in expired:
            handle = self.active.pop(infohash, None)
            self.attempt_started.pop(infohash, None)
            db.upsert_record(
                IndexRecord(
                    infohash=infohash,
                    metadata_status="timeout",
                    error=f"metadata timeout after {self.config.metadata_timeout}s",
                )
            )
            if handle is not None:
                self._remove_torrent(handle)

    def _remove_torrent(self, handle: object) -> None:
        try:
            self.session.remove_torrent(handle)
        except Exception:
            return

    @staticmethod
    def _sha1_hash() -> object:
        return lt.sha1_hash(os.urandom(20))  # type: ignore[union-attr]


def _alert_name(alert: object) -> str:
    if hasattr(alert, "what"):
        try:
            return str(alert.what()).lower()
        except TypeError:
            pass
    return type(alert).__name__.lower()


def _alert_message(alert: object) -> str:
    if hasattr(alert, "message"):
        try:
            return str(alert.message())
        except TypeError:
            pass
    return repr(alert)


def _alert_values(alert: object, name: str) -> list[object]:
    if not hasattr(alert, name):
        return []
    value = getattr(alert, name)
    if callable(value):
        value = value()
    if value is None:
        return []
    return list(value)


def _node_endpoint(node: object) -> object | None:
    if isinstance(node, dict) and "endpoint" in node:
        return node["endpoint"]
    if isinstance(node, tuple) and len(node) >= 2:
        return node[1]
    if hasattr(node, "endpoint"):
        return getattr(node, "endpoint")
    return None


def _sha1_to_hex(value: object) -> str:
    if isinstance(value, bytes):
        return value.hex()
    text = str(value)
    if len(text) == 40:
        return text.lower()
    if hasattr(value, "to_bytes"):
        return value.to_bytes().hex()
    raise ValueError(f"Cannot convert SHA1 value to hex: {value!r}")


def _handle_infohash(handle: object) -> str:
    if hasattr(handle, "info_hash"):
        return _sha1_to_hex(handle.info_hash())
    if hasattr(handle, "info_hashes"):
        hashes = handle.info_hashes()
        if hasattr(hashes, "v1") and hashes.v1:
            return _sha1_to_hex(hashes.v1)
    status = handle.status()
    if hasattr(status, "info_hash"):
        return _sha1_to_hex(status.info_hash)
    raise ValueError("Unable to extract infohash from torrent handle.")


def record_from_metadata(metadata: TorrentMetadata) -> IndexRecord | None:
    if metadata.creation_date is not None and metadata.creation_date >= AI_CUTOFF_TIMESTAMP:
        return None
    return metadata.to_record()
