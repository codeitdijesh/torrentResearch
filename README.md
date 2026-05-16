# Lazarus Indexer

Metadata-only BitTorrent swarm crawler for Project Lazarus. It indexes legacy torrent metadata into SQLite so pre-LLM-era data opportunities can be measured without downloading payload content.

## Setup

```powershell
python -m pip install -r requirements.txt
```

`libtorrent` Python bindings are platform-specific. If `pip` cannot install them on Windows, install the bindings through a compatible wheel, Conda, MSYS2, or WSL package source.

## Commands

```powershell
python main.py --db lazarus.db init-db
python main.py init-db --db lazarus.db
python main.py ingest --db lazarus.db --torrent-dir .\samples
python main.py ingest --db lazarus.db --magnet "magnet:?xt=urn:btih:<40-hex-infohash>"
python main.py crawl --db lazarus.db --duration-seconds 300 --max-infohashes 1000
```

## Data Model

The primary table is `lazarus_index`. `infohash` is the primary key and deduplication layer. The crawler also records swarm fields (`seeds`, `peers`, `leechers`) plus operational fields (`source`, `metadata_status`, `error`, timestamps).

The cutoff is `2022-01-01T00:00:00Z`. Local `.torrent` files created on or after that timestamp are discarded. Magnet and DHT metadata exchange does not expose the original top-level `creation date`, so those rows are kept as `metadata_without_creation_date` until richer metadata is available.
