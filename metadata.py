from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from database import IndexRecord


try:
    import bencodepy
except ImportError:  # pragma: no cover - exercised only on machines without deps.
    bencodepy = None  # type: ignore[assignment]


AI_CUTOFF = datetime(2022, 1, 1, tzinfo=timezone.utc)
AI_CUTOFF_TIMESTAMP = int(AI_CUTOFF.timestamp())
HEX_INFOHASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")
BASE32_INFOHASH_RE = re.compile(r"^[A-Z2-7a-z]{32}$")


class MetadataError(ValueError):
    pass


class DependencyMissingError(RuntimeError):
    pass


@dataclass(slots=True)
class TorrentMetadata:
    infohash: str
    name: str | None
    size_bytes: int | None
    creation_date: int | None
    source: str
    metadata_status: str = "complete"
    error: str | None = None

    @property
    def is_pre_llm_or_unknown(self) -> bool:
        return self.creation_date is None or self.creation_date < AI_CUTOFF_TIMESTAMP

    def to_record(self) -> IndexRecord:
        return IndexRecord(
            infohash=self.infohash,
            name=self.name,
            size_bytes=self.size_bytes,
            creation_date=self.creation_date,
            source=self.source,
            metadata_status=self.metadata_status,
            error=self.error,
        )


def ensure_bencodepy() -> None:
    if bencodepy is None:
        raise DependencyMissingError(
            "bencodepy is not installed. Install dependencies with "
            "`python -m pip install -r requirements.txt`."
        )


def parse_torrent_file(path: str | Path) -> TorrentMetadata | None:
    torrent_path = Path(path)
    return parse_torrent_bytes(torrent_path.read_bytes(), source=str(torrent_path))


def parse_torrent_bytes(raw: bytes, source: str = "torrent") -> TorrentMetadata | None:
    ensure_bencodepy()
    decoded = bencodepy.decode(raw)  # type: ignore[union-attr]
    if not isinstance(decoded, dict):
        raise MetadataError("Torrent metainfo must be a bencoded dictionary.")

    info = _get(decoded, b"info")
    if not isinstance(info, dict):
        raise MetadataError("Torrent metainfo is missing an info dictionary.")

    raw_info = extract_top_level_value(raw, b"info")
    infohash = hashlib.sha1(raw_info).hexdigest()
    creation_date = _optional_int(_get(decoded, b"creation date", default=None))
    if creation_date is not None and creation_date >= AI_CUTOFF_TIMESTAMP:
        return None

    name = _optional_text(_get(info, b"name.utf-8", default=None)) or _optional_text(
        _get(info, b"name", default=None)
    )
    size_bytes = _extract_size(info)
    return TorrentMetadata(
        infohash=infohash,
        name=name,
        size_bytes=size_bytes,
        creation_date=creation_date,
        source=source,
    )


def parse_magnet_link(magnet: str, source: str = "magnet") -> TorrentMetadata:
    infohash = extract_btih(magnet)
    display_name = None
    parsed = urlparse(magnet)
    params = parse_qs(parsed.query)
    if "dn" in params and params["dn"]:
        display_name = unquote(params["dn"][0])
    return TorrentMetadata(
        infohash=infohash,
        name=display_name,
        size_bytes=None,
        creation_date=None,
        source=source,
        metadata_status="pending",
    )


def extract_btih(magnet: str) -> str:
    parsed = urlparse(magnet)
    if parsed.scheme != "magnet":
        raise MetadataError("Magnet link must start with magnet:?")
    params = parse_qs(parsed.query)
    xt_values = params.get("xt", [])
    for xt_value in xt_values:
        lowered = xt_value.lower()
        if lowered.startswith("urn:btih:"):
            raw_hash = xt_value.split(":")[-1]
            return normalize_btih(raw_hash)
    raise MetadataError("Magnet link is missing an xt=urn:btih value.")


def normalize_btih(value: str) -> str:
    stripped = value.strip()
    if HEX_INFOHASH_RE.fullmatch(stripped):
        return stripped.lower()
    if BASE32_INFOHASH_RE.fullmatch(stripped):
        padded = stripped.upper() + "=" * ((8 - len(stripped) % 8) % 8)
        return base64.b32decode(padded).hex()
    raise MetadataError(f"Unsupported btih value: {value!r}")


def extract_top_level_value(raw: bytes, key: bytes) -> bytes:
    parser = _BencodeSpanParser(raw)
    return parser.top_level_dict_value(key)


def _extract_size(info: dict[object, object]) -> int | None:
    single_length = _optional_int(_get(info, b"length", default=None))
    if single_length is not None:
        return single_length

    files = _get(info, b"files", default=None)
    if not isinstance(files, list):
        return None

    total = 0
    saw_length = False
    for item in files:
        if not isinstance(item, dict):
            continue
        length = _optional_int(_get(item, b"length", default=None))
        if length is not None:
            total += length
            saw_length = True
    return total if saw_length else None


def _get(mapping: dict[object, object], key: bytes, default: object = None) -> object:
    if key in mapping:
        return mapping[key]
    text_key = key.decode("utf-8", errors="ignore")
    return mapping.get(text_key, default)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        return int(value.decode("ascii"))
    if isinstance(value, str):
        return int(value)
    return None


class _BencodeSpanParser:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def top_level_dict_value(self, wanted_key: bytes) -> bytes:
        if not self.raw.startswith(b"d"):
            raise MetadataError("Torrent metainfo must start with a bencoded dictionary.")
        pos = 1
        while pos < len(self.raw):
            if self.raw[pos : pos + 1] == b"e":
                break
            key_start = pos
            key_end = self._skip(pos)
            key = self.raw[key_start:key_end]
            key_decoded = self._decode_string_token(key)
            value_start = key_end
            value_end = self._skip(value_start)
            if key_decoded == wanted_key:
                return self.raw[value_start:value_end]
            pos = value_end
        raise MetadataError(f"Torrent metainfo is missing key {wanted_key!r}.")

    def _skip(self, pos: int) -> int:
        token = self.raw[pos : pos + 1]
        if token == b"i":
            end = self.raw.find(b"e", pos)
            if end == -1:
                raise MetadataError("Unterminated bencoded integer.")
            return end + 1
        if token == b"l" or token == b"d":
            pos += 1
            while pos < len(self.raw) and self.raw[pos : pos + 1] != b"e":
                pos = self._skip(pos)
            if pos >= len(self.raw):
                raise MetadataError("Unterminated bencoded list/dictionary.")
            return pos + 1
        if token.isdigit():
            colon = self.raw.find(b":", pos)
            if colon == -1:
                raise MetadataError("Invalid bencoded string.")
            length = int(self.raw[pos:colon])
            return colon + 1 + length
        raise MetadataError(f"Unexpected bencode token {token!r} at byte {pos}.")

    @staticmethod
    def _decode_string_token(token: bytes) -> bytes:
        colon = token.find(b":")
        if colon == -1:
            raise MetadataError("Invalid bencoded dictionary key.")
        length = int(token[:colon])
        value = token[colon + 1 :]
        if len(value) != length:
            raise MetadataError("Invalid bencoded dictionary key length.")
        return value

