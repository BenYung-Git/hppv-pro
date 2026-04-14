from __future__ import annotations

from pathlib import Path

from hppv.db import fetch_race_records, init_db, upsert_race_records
from hppv.models import RaceRecord


def test_db_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "t.sqlite3"
    init_db(db_path)
    inserted = upsert_race_records(
        db_path,
        [
            RaceRecord(
                horse_id="H1",
                race_date="2026-01-01",
                venue="ST",
                race_no="1",
                course="A",
                distance="1200",
                going="Good",
                class_band="Class 3",
                draw="1",
                rating="50",
                jockey="J",
                trainer="T",
                finish_pos="1",
                time="1:09.99",
                prize="1000",
            )
        ],
    )
    assert inserted == 1
    rows = fetch_race_records(db_path, "H1")
    assert len(rows) == 1
    assert rows[0].horse_id == "H1"

