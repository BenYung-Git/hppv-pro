"""馬匹資料：HKJC rating list → horses insert / fill blanks."""

from __future__ import annotations

from pathlib import Path

from hppv.db import fetch_horse_by_id, init_db, insert_or_fill_horse, upsert_horses
from hppv.racing_download import run_horses_download
from hppv.racing_requests import (
    HORSES_QUERY,
    REQUEST_KEY_HORSES,
    RacingRequestPresetError,
    RacingRequestPayload,
    assert_payload_matches_whitelist,
    ensure_racing_preset_files,
    expected_query_for_preset,
    load_racing_request_preset,
)
from hppv.scraper import HorseBasicInfo


def test_insert_or_fill_horse_insert_then_skip(tmp_path: Path) -> None:
    db = tmp_path / "hppv.sqlite3"
    init_db(db)
    row = {
        "horse_id": "HK_2024_K406",
        "brand_no": "K406",
        "name_zh": "知道穩勝",
        "name_en": "Knowing Steady",
        "status": None,
    }
    assert insert_or_fill_horse(db, row) == "inserted"
    assert insert_or_fill_horse(db, row) == "skipped"
    horse = fetch_horse_by_id(db, "HK_2024_K406")
    assert horse is not None
    assert str(horse["name_zh"]) == "知道穩勝"


def test_insert_or_fill_horse_fills_blank_and_placeholder(tmp_path: Path) -> None:
    db = tmp_path / "hppv.sqlite3"
    init_db(db)
    # Legacy placeholder row (name_en == brand only).
    upsert_horses(
        db,
        [
            {
                "horse_id": "HK_2025_L435",
                "brand_no": "L435",
                "name_zh": None,
                "name_en": "L435",
                "status": None,
            }
        ],
    )
    assert (
        insert_or_fill_horse(
            db,
            {
                "horse_id": "HK_2025_L435",
                "brand_no": "L435",
                "name_zh": "風耀奪標",
                "name_en": "Fung Yiu Duet",
                "status": None,
            },
        )
        == "filled"
    )
    horse = fetch_horse_by_id(db, "HK_2025_L435")
    assert horse is not None
    assert str(horse["name_zh"]) == "風耀奪標"
    assert str(horse["name_en"]) == "Fung Yiu Duet"

    # Real names must not be overwritten.
    assert (
        insert_or_fill_horse(
            db,
            {
                "horse_id": "HK_2025_L435",
                "brand_no": "L435",
                "name_zh": "不應覆寫",
                "name_en": "ShouldNotOverwrite",
            },
        )
        == "skipped"
    )
    kept = fetch_horse_by_id(db, "HK_2025_L435")
    assert kept is not None
    assert str(kept["name_zh"]) == "風耀奪標"
    assert str(kept["name_en"]) == "Fung Yiu Duet"


def test_horses_download_insert_fill_then_skip(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "hppv.sqlite3"
    init_db(db)
    upsert_horses(
        db,
        [
            {
                "horse_id": "HK_2025_L435",
                "brand_no": "L435",
                "name_zh": None,
                "name_en": "L435",
            }
        ],
    )

    sample = [
        HorseBasicInfo(
            horse_id="HK_2024_K406",
            brand_no="K406",
            name_zh="知道穩勝",
            name_en="Knowing Steady",
        ),
        HorseBasicInfo(
            horse_id="HK_2025_L435",
            brand_no="L435",
            name_zh="風耀奪標",
            name_en="Fung Yiu Duet",
        ),
    ]
    monkeypatch.setattr(
        "hppv.racing_download.fetch_all_horses_basic_info",
        lambda: sample,
    )

    first = run_horses_download(db)
    assert first.code == "success"
    assert first.progress.updated == 2
    assert first.progress.skipped == 0
    assert first.detail.get("write_mode") == "insert_or_fill_blanks"
    assert first.detail.get("inserted") == 1
    assert first.detail.get("filled") == 1
    filled = fetch_horse_by_id(db, "HK_2025_L435")
    assert filled is not None
    assert str(filled["name_zh"]) == "風耀奪標"

    second = run_horses_download(db)
    assert second.code == "success"
    assert second.progress.updated == 0
    assert second.progress.skipped == 2
    assert second.detail.get("inserted") == 0
    assert second.detail.get("filled") == 0


def test_horses_preset_whitelist(tmp_path: Path) -> None:
    requests_dir = tmp_path / "racing_requests"
    ensure_racing_preset_files(requests_dir)
    assert expected_query_for_preset(REQUEST_KEY_HORSES) == HORSES_QUERY
    payload, display = load_racing_request_preset(requests_dir, REQUEST_KEY_HORSES)
    assert_payload_matches_whitelist(REQUEST_KEY_HORSES, payload)
    assert payload.operation_name == "horseRatingList"
    assert isinstance(payload.variables, dict)
    import json

    json.loads(display)
    bad = RacingRequestPayload(
        query="not-whitelisted",
        variables={},
        operation_name="horseRatingList",
    )
    try:
        assert_payload_matches_whitelist(REQUEST_KEY_HORSES, bad)
        raise AssertionError("expected query_not_whitelisted")
    except RacingRequestPresetError as e:
        assert e.code == "query_not_whitelisted"
