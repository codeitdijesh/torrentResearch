from __future__ import annotations

import argparse
import sys
from pathlib import Path

from crawler import CrawlerConfig, LibtorrentMissingError, SwarmCrawler
from database import LazarusDatabase
from metadata import DependencyMissingError, MetadataError, parse_magnet_link, parse_torrent_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazarus-indexer",
        description="Metadata-only BitTorrent swarm crawler for Project Lazarus.",
    )
    parser.add_argument("--db", default="lazarus.db", help="SQLite database path.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="Create or update the SQLite schema.")
    add_db_override(init_db)

    ingest = subparsers.add_parser("ingest", help="Ingest .torrent files and/or magnet links.")
    add_db_override(ingest)
    ingest.add_argument("--torrent", action="append", default=[], help="Path to a .torrent file.")
    ingest.add_argument("--torrent-dir", action="append", default=[], help="Directory of .torrent files.")
    ingest.add_argument("--magnet", action="append", default=[], help="Magnet URI.")
    ingest.add_argument("--magnet-file", action="append", default=[], help="Text file with one magnet URI per line.")

    crawl = subparsers.add_parser("crawl", help="Run a bounded metadata-only DHT crawl.")
    add_db_override(crawl)
    crawl.add_argument("--duration-seconds", type=int, default=300)
    crawl.add_argument("--max-infohashes", type=int, default=1000)
    crawl.add_argument("--metadata-timeout", type=int, default=45)
    crawl.add_argument("--request-interval", type=float, default=1.0)
    crawl.add_argument("--temp-path", default=None, help="Temporary save path for libtorrent metadata state.")

    return parser


def add_db_override(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help="SQLite database path. May be placed before or after the subcommand.",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)

    try:
        if args.command == "init-db":
            with LazarusDatabase(db_path) as db:
                db.initialize()
                print(f"initialized {db_path}")
            return 0

        if args.command == "ingest":
            return run_ingest(args, db_path)

        if args.command == "crawl":
            config = CrawlerConfig(
                db_path=db_path,
                duration_seconds=args.duration_seconds,
                max_infohashes=args.max_infohashes,
                metadata_timeout=args.metadata_timeout,
                request_interval=args.request_interval,
                temp_path=Path(args.temp_path) if args.temp_path else None,
            )
            crawler = SwarmCrawler(config)
            summary = crawler.crawl()
            print(
                "crawl complete: "
                f"{summary['total']} total, "
                f"{summary['recoverable']} recoverable, "
                f"{summary['timestamped']} timestamped"
            )
            return 0

        raise AssertionError(f"Unhandled command: {args.command}")
    except (DependencyMissingError, LibtorrentMissingError, MetadataError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def run_ingest(args: argparse.Namespace, db_path: Path) -> int:
    torrent_paths = collect_torrent_paths(args.torrent, args.torrent_dir)
    magnets = collect_magnets(args.magnet, args.magnet_file)
    inserted = 0
    discarded = 0

    with LazarusDatabase(db_path) as db:
        db.initialize()
        for torrent_path in torrent_paths:
            metadata = parse_torrent_file(torrent_path)
            if metadata is None:
                discarded += 1
                continue
            db.upsert_record(metadata.to_record())
            inserted += 1
        for magnet in magnets:
            metadata = parse_magnet_link(magnet)
            db.upsert_record(metadata.to_record())
            inserted += 1
        summary = db.summary()

    print(
        "ingest complete: "
        f"{inserted} inserted/updated, {discarded} discarded by cutoff, "
        f"{summary['total']} total indexed"
    )
    return 0


def collect_torrent_paths(files: list[str], dirs: list[str]) -> list[Path]:
    paths = [Path(item) for item in files]
    for directory in dirs:
        paths.extend(sorted(Path(directory).glob("*.torrent")))
    return paths


def collect_magnets(values: list[str], files: list[str]) -> list[str]:
    magnets = list(values)
    for path in files:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                magnets.append(stripped)
    return magnets


if __name__ == "__main__":
    raise SystemExit(main())
