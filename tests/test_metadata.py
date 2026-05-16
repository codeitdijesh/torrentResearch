from __future__ import annotations

import hashlib

import pytest

import metadata
from metadata import AI_CUTOFF_TIMESTAMP, extract_btih, parse_magnet_link, parse_torrent_bytes


pytestmark = pytest.mark.skipif(metadata.bencodepy is None, reason="bencodepy is not installed")


def _torrent_bytes(creation_date: int, info: dict[bytes, object]) -> bytes:
    return metadata.bencodepy.encode(  # type: ignore[union-attr]
        {
            b"announce": b"udp://tracker.example:80",
            b"creation date": creation_date,
            b"info": info,
        }
    )


def test_parse_single_file_torrent_pre_cutoff():
    info = {b"name": b"archive.tar", b"length": 1234, b"piece length": 16384, b"pieces": b"0" * 20}
    raw = _torrent_bytes(AI_CUTOFF_TIMESTAMP - 1, info)
    parsed = parse_torrent_bytes(raw)

    assert parsed is not None
    assert parsed.infohash == hashlib.sha1(metadata.bencodepy.encode(info)).hexdigest()  # type: ignore[union-attr]
    assert parsed.name == "archive.tar"
    assert parsed.size_bytes == 1234


def test_parse_multi_file_size():
    info = {
        b"name": b"dataset",
        b"piece length": 16384,
        b"pieces": b"0" * 20,
        b"files": [{b"length": 10, b"path": [b"a"]}, {b"length": 15, b"path": [b"b"]}],
    }
    parsed = parse_torrent_bytes(_torrent_bytes(AI_CUTOFF_TIMESTAMP - 10, info))

    assert parsed is not None
    assert parsed.size_bytes == 25


def test_cutoff_discards_post_2022_torrent():
    info = {b"name": b"new-data", b"length": 99, b"piece length": 16384, b"pieces": b"0" * 20}

    assert parse_torrent_bytes(_torrent_bytes(AI_CUTOFF_TIMESTAMP, info)) is None


def test_parse_magnet_hex_btih():
    infohash = "0123456789abcdef0123456789abcdef01234567"
    parsed = parse_magnet_link(f"magnet:?xt=urn:btih:{infohash}&dn=example")

    assert parsed.infohash == infohash
    assert parsed.name == "example"
    assert parsed.metadata_status == "pending"


def test_extract_base32_btih():
    raw = bytes.fromhex("0123456789abcdef0123456789abcdef01234567")
    btih = metadata.base64.b32encode(raw).decode("ascii").rstrip("=")

    assert extract_btih(f"magnet:?xt=urn:btih:{btih}") == raw.hex()

