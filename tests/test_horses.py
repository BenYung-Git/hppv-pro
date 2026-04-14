from __future__ import annotations

from pathlib import Path

from hppv.db import init_db, search_horses_by_name, upsert_horses


def test_horses_upsert_and_search(tmp_path: Path) -> None:
    db_path = tmp_path / "t.sqlite3"
    init_db(db_path)

    inserted = upsert_horses(
        db_path,
        [
            {
                "horse_id": "HK_1",
                "name_zh": "測試馬",
                "name_en": "Test Horse",
                "status": None,
            }
        ],
    )
    assert inserted == 1

    rows = search_horses_by_name(db_path, "測試")
    assert len(rows) == 1
    assert rows[0]["horse_id"] == "HK_1"

    rows_en = search_horses_by_name(db_path, "Test")
    assert len(rows_en) == 1
    assert rows_en[0]["horse_id"] == "HK_1"

