from __future__ import annotations

from database import IndexRecord, LazarusDatabase


def test_upsert_deduplicates_by_infohash():
    db_path = ":memory:"
    infohash = "a" * 40

    with LazarusDatabase(db_path) as db:
        db.initialize()
        db.upsert_record(IndexRecord(infohash=infohash, name="first", seeds=0))
        db.upsert_record(IndexRecord(infohash=infohash.upper(), name="second", seeds=3))
        summary = db.summary()

    assert summary["total"] == 1
    assert summary["recoverable"] == 1
