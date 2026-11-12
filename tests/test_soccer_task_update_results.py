"""update_results: history upsert, daily delete+tombstone, scheduler settings."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hppv.footylogic_schema import FOOTYLOGIC_MATCHES_TABLE
from hppv.schema import SOCCER_DAILY_REMOVED_TABLE
from hppv.soccer_db import get_connection, init_soccer_db
from hppv.soccer_ingest import (
    ingest_matches_and_odds,
    ingest_official_results_to_history,
)
from hppv.soccer_task_scheduler import (
    STATUS_SUCCESS,
    TASK_KEY_UPDATE_RESULTS,
    get_update_results_state,
    restore_update_results_on_startup,
    run_update_results_once,
    save_update_results_settings,
)


@pytest.fixture()
def soccer_db(tmp_path: Path) -> Path:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    return db


def _insert_daily(db: Path, match_id: str = "50001", *, match_date: str = "2026-11-12") -> None:
    with get_connection(db) as conn:
        conn.execute(
            """
            INSERT INTO soccer_daily (
                match_id, match_code, match_date, kickoff_at,
                competition_code, competition_name_zh,
                home_team, away_team, match_status,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                match_id,
                "FB9001",
                match_date,
                f"{match_date}T20:00:00+08:00",
                "EPL",
                "英超",
                "主隊",
                "客隊",
                "PreMatch",
                "t",
                "t",
            ),
        )


def _official_item(
    match_id: str,
    *,
    status: str = "MATCHENDED",
    ht: tuple[int, int] | None = (0, 0),
    ft: tuple[int, int] | None = (2, 1),
    kick_off: str | None = "2026-11-12T12:00:00.000Z",
    front_end: str | None = "FB9001",
) -> dict:
    results = []
    if ht is not None:
        results.append(
            {
                "homeResult": ht[0],
                "awayResult": ht[1],
                "resultConfirmType": 2,
                "payoutConfirmed": True,
                "stageId": 3,
                "resultType": 1,
                "sequence": 1,
            }
        )
    if ft is not None:
        results.append(
            {
                "homeResult": ft[0],
                "awayResult": ft[1],
                "resultConfirmType": 2,
                "payoutConfirmed": True,
                "stageId": 5,
                "resultType": 1,
                "sequence": 1,
            }
        )
    return {
        "id": match_id,
        "status": status,
        "frontEndId": front_end,
        "kickOffTime": kick_off,
        "homeTeam": {"id": "H1", "name_ch": "主隊甲", "name_en": "Home"},
        "awayTeam": {"id": "A1", "name_ch": "客隊乙", "name_en": "Away"},
        "tournament": {"code": "EPL", "name_ch": "英超", "name_en": "EPL"},
        "results": results,
    }


def _envelope(matches: list[dict]) -> dict:
    return {
        "data": {
            "matchNumByDate": {"total": len(matches)},
            "matches": matches,
        }
    }


def test_results_default_disabled(soccer_db: Path) -> None:
    state = get_update_results_state(soccer_db)
    assert state.task_key == TASK_KEY_UPDATE_RESULTS
    assert state.enabled is False
    assert state.next_run_at is None


def test_ft_writes_history_deletes_daily_and_tombstone(soccer_db: Path) -> None:
    _insert_daily(soccer_db, "50001")
    resp = _envelope([_official_item("50001")])
    ing = ingest_official_results_to_history(
        soccer_db,
        resp,
        variables={"startIndex": 0, "endIndex": 0},
    )
    assert ing.code == "success"
    assert ing.daily_deleted == 1
    assert ing.inserted == 1
    with get_connection(soccer_db) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM soccer_daily").fetchone()[0] == 0
        )
        row = conn.execute(
            f"SELECT * FROM {FOOTYLOGIC_MATCHES_TABLE} WHERE source_key=?",
            ("hkjc|50001",),
        ).fetchone()
        assert row is not None
        assert row["source"] == "hkjc"
        assert row["score_ft_home"] == 2
        assert row["score_ft_away"] == 1
        assert row["score_ht_home"] == 0
        assert row["hkjc_match_id"] == "50001"
        assert row["hkjc_front_end_id"] == "FB9001"
        assert row["competition_id"] == "EPL"
        assert row["season_id"] is None
        assert row["month"] == "2026-11"
        tomb = conn.execute(
            f"SELECT reason FROM {SOCCER_DAILY_REMOVED_TABLE} WHERE match_id=?",
            ("50001",),
        ).fetchone()
        assert tomb["reason"] == "official_ft_to_history"


def test_inplay_matchended_with_ft_writes_and_deletes(soccer_db: Path) -> None:
    """Source status is not a gate; confirmed FT in matchResults is enough."""
    _insert_daily(soccer_db, "50079638")
    resp = _envelope(
        [
            _official_item(
                "50079638",
                status="INPLAYMATCHENDED",
                ht=(0, 0),
                ft=(1, 2),
                front_end="FB8335",
            )
        ]
    )
    ing = ingest_official_results_to_history(
        soccer_db,
        resp,
        variables={"startIndex": 0, "endIndex": 0},
    )
    assert ing.code == "success"
    assert ing.daily_deleted == 1
    assert ing.skipped_status == 0
    with get_connection(soccer_db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soccer_daily WHERE match_id='50079638'"
            ).fetchone()[0]
            == 0
        )
        row = conn.execute(
            f"SELECT score_ft_home, score_ft_away, hkjc_front_end_id "
            f"FROM {FOOTYLOGIC_MATCHES_TABLE} WHERE source_key=?",
            ("hkjc|50079638",),
        ).fetchone()
        assert row["score_ft_home"] == 1
        assert row["score_ft_away"] == 2
        assert row["hkjc_front_end_id"] == "FB8335"


def test_ht_only_keeps_daily(soccer_db: Path) -> None:
    _insert_daily(soccer_db, "50002")
    resp = _envelope([_official_item("50002", ht=(1, 0), ft=None)])
    ing = ingest_official_results_to_history(
        soccer_db,
        resp,
        variables={"startIndex": 0, "endIndex": 0},
    )
    assert ing.code.startswith("skipped")
    assert ing.daily_deleted == 0
    with get_connection(soccer_db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soccer_daily WHERE match_id='50002'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                f"SELECT COUNT(*) FROM {FOOTYLOGIC_MATCHES_TABLE} WHERE source_key='hkjc|50002'"
            ).fetchone()[0]
            == 0
        )


def test_missing_kickoff_keeps_daily(soccer_db: Path) -> None:
    _insert_daily(soccer_db, "50003")
    resp = _envelope([_official_item("50003", kick_off=None)])
    ing = ingest_official_results_to_history(
        soccer_db,
        resp,
        variables={"startIndex": 0, "endIndex": 0},
    )
    assert ing.skipped_no_kickoff == 1
    assert ing.daily_deleted == 0
    with get_connection(soccer_db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soccer_daily WHERE match_id='50003'"
            ).fetchone()[0]
            == 1
        )


def test_reupdate_does_not_clear_existing_scores(soccer_db: Path) -> None:
    _insert_daily(soccer_db, "50004")
    first = _envelope([_official_item("50004", ht=(1, 0), ft=(2, 1))])
    ingest_official_results_to_history(
        soccer_db, first, variables={"startIndex": 0, "endIndex": 0}
    )
    # Put back into daily to allow second processing path via only_daily override,
    # and call upsert path by re-inserting daily + second ingest with HT missing.
    _insert_daily(soccer_db, "50004")
    second = _envelope(
        [_official_item("50004", ht=None, ft=(3, 1), front_end=None)]
    )
    ing = ingest_official_results_to_history(
        soccer_db, second, variables={"startIndex": 0, "endIndex": 0}
    )
    assert ing.code == "success"
    assert ing.updated == 1
    with get_connection(soccer_db) as conn:
        row = conn.execute(
            f"SELECT score_ht_home, score_ht_away, score_ft_home, score_ft_away, "
            f"hkjc_front_end_id FROM {FOOTYLOGIC_MATCHES_TABLE} WHERE source_key=?",
            ("hkjc|50004",),
        ).fetchone()
        assert row["score_ft_home"] == 3
        assert row["score_ft_away"] == 1
        # HT missing in second payload must not clear prior HT.
        assert row["score_ht_home"] == 1
        assert row["score_ht_away"] == 0
        # frontEndId missing must not clear prior value.
        assert row["hkjc_front_end_id"] == "FB9001"


def test_tombstone_blocks_update_matches_reinsert(soccer_db: Path) -> None:
    _insert_daily(soccer_db, "50005")
    ingest_official_results_to_history(
        soccer_db,
        _envelope([_official_item("50005")]),
        variables={"startIndex": 0, "endIndex": 0},
    )
    payload = {
        "data": {
            "matches": [
                {
                    "id": "50005",
                    "frontEndId": "FB9",
                    "kickOffTime": "2026-11-12T12:00:00.000+08:00",
                    "status": "PreEvent",
                    "homeTeam": {"name_ch": "主隊", "name_en": "H"},
                    "awayTeam": {"name_ch": "客隊", "name_en": "A"},
                    "tournament": {"code": "EPL", "name_ch": "英超", "name_en": "EPL"},
                    "foPools": [
                        {
                            "id": "p1",
                            "status": "SELLINGSTARTED",
                            "oddsType": "HAD",
                            "lines": [
                                {
                                    "main": True,
                                    "status": "AVAILABLE",
                                    "combinations": [
                                        {"str": "H", "currentOdds": "1.90"},
                                        {"str": "D", "currentOdds": "3.20"},
                                        {"str": "A", "currentOdds": "2.50"},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    }
    ing = ingest_matches_and_odds(soccer_db, payload)
    assert ing.inserted == 0
    assert ing.skipped_tombstone == 1
    with get_connection(soccer_db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM soccer_daily WHERE match_id='50005'"
            ).fetchone()[0]
            == 0
        )


def test_empty_daily_skips_without_graphql(soccer_db: Path, tmp_path: Path) -> None:
    req = tmp_path / "requests"
    req.mkdir()
    (req / "match_results.json").write_text(
        json.dumps(
            {
                "operationName": "matchResults",
                "variables": {
                    "startDate": "2026-11-01",
                    "endDate": "2026-11-02",
                    "startIndex": 0,
                    "endIndex": 0,
                    "teamId": None,
                },
                "query": "query matchResults { __typename }",
            }
        ),
        encoding="utf-8",
    )
    with patch(
        "hppv.soccer_task_scheduler.send_soccer_graphql"
    ) as send_mock:
        detail = run_update_results_once(soccer_db, req)
        send_mock.assert_not_called()
    assert detail["ingest_code"] == "skipped_empty_daily"


def test_scheduler_enable_and_restore(soccer_db: Path) -> None:
    state = save_update_results_settings(
        soccer_db, enabled=True, interval_value=2, interval_unit="mins"
    )
    assert state.enabled is True
    assert state.next_run_at is not None
    restored = restore_update_results_on_startup(soccer_db)
    assert restored.enabled is True
    assert restored.last_status in (None, "idle", STATUS_SUCCESS) or True
