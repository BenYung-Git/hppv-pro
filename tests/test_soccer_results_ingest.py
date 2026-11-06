from __future__ import annotations

import json
from pathlib import Path

import pytest

from hppv.soccer_db import get_connection, init_soccer_db
from hppv.soccer_ingest import (
    REQUEST_KEY_MATCH_RESULTS,
    OfficialScorePair,
    SoccerIngestError,
    get_last_success_at,
    ingest_official_match_results,
    parse_official_result_item,
    select_official_stage_score,
)


def _seed_match(db: Path, match_id: str = "50079013") -> None:
    init_soccer_db(db)
    with get_connection(db) as conn:
        conn.execute(
            """
            INSERT INTO soccer_daily (
              match_id, match_code, match_date, kickoff_at,
              competition_code, competition_name_zh, home_team, away_team,
              match_status, created_at, updated_at
            ) VALUES (?, 'FB8287', '2026-11-05', '2026-11-05T20:00:00+08:00',
                      'EPL', '聯賽', '主隊', '客隊', 'PREEVENT',
                      '2026-11-04T00:00:00+00:00', '2026-11-04T00:00:00+00:00');
            """,
            (match_id,),
        )
        conn.commit()


def _result_row(
    *,
    home: int,
    away: int,
    stage_id: int,
    sequence: int,
    result_type: int = 1,
    confirm: int = 5,
    payout: bool = True,
) -> dict:
    return {
        "homeResult": home,
        "awayResult": away,
        "ttlCornerResult": -1,
        "resultConfirmType": confirm,
        "payoutConfirmed": payout,
        "stageId": stage_id,
        "resultType": result_type,
        "sequence": sequence,
    }


def _payload(matches: list[dict], total: int | None = None) -> dict:
    data: dict = {"matches": matches}
    if total is not None:
        data["matchNumByDate"] = {"total": total}
    return {"data": data}


def test_select_max_sequence_and_reject_conflict() -> None:
    rows = [
        _result_row(home=0, away=1, stage_id=3, sequence=50),
        _result_row(home=0, away=1, stage_id=3, sequence=51),
    ]
    assert select_official_stage_score(rows, stage_id=3, match_id="1") == OfficialScorePair(
        home=0, away=1
    )

    conflict = rows + [_result_row(home=1, away=0, stage_id=3, sequence=51)]
    with pytest.raises(SoccerIngestError) as exc:
        select_official_stage_score(conflict, stage_id=3, match_id="1")
    assert exc.value.code == "result_conflict"


def test_ch_conditions_reject_unconfirmed_and_corners() -> None:
    rows = [
        _result_row(home=1, away=0, stage_id=3, sequence=1, confirm=1),
        _result_row(home=2, away=0, stage_id=3, sequence=2, payout=False),
        _result_row(home=9, away=9, stage_id=3, sequence=3, result_type=2),
        _result_row(home=0, away=1, stage_id=3, sequence=4),
    ]
    pair = select_official_stage_score(rows, stage_id=3, match_id="1")
    assert pair is not None
    assert (pair.home, pair.away) == (0, 1)


def test_zero_zero_and_second_half() -> None:
    item = {
        "id": "50079013",
        "status": "MATCHENDED",
        "results": [
            _result_row(home=0, away=0, stage_id=3, sequence=1),
            _result_row(home=0, away=0, stage_id=5, sequence=2),
        ],
    }
    parsed = parse_official_result_item(item)
    assert parsed.ht is not None and (parsed.ht.home, parsed.ht.away) == (0, 0)
    assert parsed.ft is not None and (parsed.ft.home, parsed.ft.away) == (0, 0)
    assert parsed.sh is not None and (parsed.sh.home, parsed.sh.away) == (0, 0)


def test_ft_less_than_ht_fails() -> None:
    item = {
        "id": "x",
        "status": "MATCHENDED",
        "results": [
            _result_row(home=2, away=1, stage_id=3, sequence=1),
            _result_row(home=1, away=1, stage_id=5, sequence=2),
        ],
    }
    with pytest.raises(SoccerIngestError) as exc:
        parse_official_result_item(item)
    assert exc.value.code == "invalid_result"


def test_ingest_matchended_writes_and_skips_others(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    _seed_match(db, "50079013")
    _seed_match(db, "known_inplay")

    matches = [
        {
            "id": "50079013",
            "status": "MATCHENDED",
            "results": [
                _result_row(home=0, away=1, stage_id=3, sequence=50),
                _result_row(home=1, away=2, stage_id=5, sequence=54),
                _result_row(home=3, away=3, stage_id=3, sequence=55, result_type=2),
            ],
        },
        {
            "id": "known_inplay",
            "status": "INPLAYMATCHENDED",
            "results": [
                _result_row(home=1, away=0, stage_id=3, sequence=1),
                _result_row(home=2, away=0, stage_id=5, sequence=2),
            ],
        },
        {
            "id": "unknown999",
            "status": "MATCHENDED",
            "results": [
                _result_row(home=1, away=1, stage_id=3, sequence=1),
                _result_row(home=2, away=2, stage_id=5, sequence=2),
            ],
        },
    ]
    payload = _payload(matches, total=3)
    stamp = "2026-11-05T03:00:00+00:00"
    result = ingest_official_match_results(
        db,
        payload,
        now_utc=stamp,
        variables={"startDate": "2026-11-04", "endDate": "2026-11-05", "startIndex": 0, "endIndex": 0},
    )
    assert result.code == "success"
    assert result.updated == 1
    assert result.ht_applied == 1
    assert result.ft_applied == 1
    assert result.sh_applied == 1
    assert result.skipped_status == 1
    assert result.skipped_missing == 1
    assert result.results_applied is True
    assert result.match_num_total == 3
    assert get_last_success_at(db, REQUEST_KEY_MATCH_RESULTS) == stamp

    with get_connection(db) as conn:
        row = conn.execute(
            "SELECT score_ht_home, score_ht_away, score_ft_home, score_ft_away, "
            "score_2h_home, score_2h_away, result_status, result_fetched_at, odds_fetched_at "
            "FROM soccer_daily WHERE match_id='50079013';"
        ).fetchone()
        assert dict(row) == {
            "score_ht_home": 0,
            "score_ht_away": 1,
            "score_ft_home": 1,
            "score_ft_away": 2,
            "score_2h_home": 1,
            "score_2h_away": 1,
            "result_status": None,
            "result_fetched_at": stamp,
            "odds_fetched_at": None,
        }
        other = conn.execute(
            "SELECT score_ft_home, result_fetched_at FROM soccer_daily WHERE match_id='known_inplay';"
        ).fetchone()
        assert other["score_ft_home"] is None
        assert other["result_fetched_at"] is None


def test_missing_stage_does_not_clear_existing(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    _seed_match(db)
    with get_connection(db) as conn:
        conn.execute(
            """
            UPDATE soccer_daily SET
              score_ht_home=1, score_ht_away=0,
              score_ft_home=2, score_ft_away=1,
              score_2h_home=1, score_2h_away=1,
              result_fetched_at='old'
            WHERE match_id='50079013';
            """
        )
        conn.commit()

    # Only HT newly confirmed; FT missing in this payload → keep old FT
    payload = _payload(
        [
            {
                "id": "50079013",
                "status": "MATCHENDED",
                "results": [_result_row(home=3, away=0, stage_id=3, sequence=10)],
            }
        ],
        total=1,
    )
    stamp = "2026-11-05T04:00:00+00:00"
    result = ingest_official_match_results(
        db, payload, now_utc=stamp, variables={"endIndex": 0}
    )
    assert result.code == "success"
    assert result.ht_applied == 1
    assert result.ft_applied == 0
    with get_connection(db) as conn:
        row = conn.execute(
            "SELECT score_ht_home, score_ht_away, score_ft_home, score_ft_away, result_fetched_at "
            "FROM soccer_daily WHERE match_id='50079013';"
        ).fetchone()
        assert (row["score_ht_home"], row["score_ht_away"]) == (3, 0)
        assert (row["score_ft_home"], row["score_ft_away"]) == (2, 1)
        assert row["result_fetched_at"] == stamp


def test_conflict_rolls_back_entire_batch(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    _seed_match(db, "50079013")
    _seed_match(db, "50079014")
    payload = _payload(
        [
            {
                "id": "50079013",
                "status": "MATCHENDED",
                "results": [
                    _result_row(home=0, away=0, stage_id=3, sequence=1),
                    _result_row(home=1, away=0, stage_id=5, sequence=2),
                ],
            },
            {
                "id": "50079014",
                "status": "MATCHENDED",
                "results": [
                    _result_row(home=1, away=0, stage_id=3, sequence=9),
                    _result_row(home=2, away=0, stage_id=3, sequence=9),
                    _result_row(home=2, away=0, stage_id=5, sequence=10),
                ],
            },
        ],
        total=2,
    )
    with pytest.raises(SoccerIngestError) as exc:
        ingest_official_match_results(db, payload, variables={"endIndex": 0})
    assert exc.value.code == "result_conflict"
    with get_connection(db) as conn:
        n = conn.execute(
            "SELECT count(*) AS c FROM soccer_daily WHERE result_fetched_at IS NOT NULL;"
        ).fetchone()["c"]
        assert n == 0
    assert get_last_success_at(db, REQUEST_KEY_MATCH_RESULTS) is None


def test_allow_insert_creates_history_and_second_pass_updates(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    payload = _payload(
        [
            {
                "id": "hist-1",
                "status": "MATCHENDED",
                "frontEndId": "FB1001",
                "kickOffTime": "2015-11-04T20:00:00.000+08:00",
                "homeTeam": {"id": "1", "name_ch": "主隊甲", "name_en": "Home A"},
                "awayTeam": {"id": "2", "name_ch": "客隊乙", "name_en": "Away B"},
                "tournament": {"code": "EPL", "name_ch": "英超", "name_en": "EPL"},
                "results": [
                    _result_row(home=0, away=1, stage_id=3, sequence=1),
                    _result_row(home=2, away=1, stage_id=5, sequence=2),
                ],
            }
        ],
        total=1,
    )
    stamp1 = "2026-11-05T06:00:00+00:00"
    r1 = ingest_official_match_results(
        db,
        payload,
        now_utc=stamp1,
        variables={"endIndex": 0},
        allow_insert=True,
        request_key=REQUEST_KEY_MATCH_RESULTS,
    )
    assert r1.code == "success"
    assert r1.inserted == 1
    assert r1.updated == 0
    with get_connection(db) as conn:
        row = conn.execute(
            "SELECT match_code, match_date, kickoff_at, home_team, away_team, "
            "competition_code, score_ht_home, score_ht_away, score_ft_home, score_ft_away, "
            "score_2h_home, score_2h_away, result_fetched_at, odds_fetched_at, home_odds, "
            "result_status FROM soccer_daily WHERE match_id='hist-1';"
        ).fetchone()
        assert row["match_code"] == "FB1001"
        assert row["match_date"] == "2015-11-04"
        assert row["kickoff_at"].startswith("2015-11-04T20:00:00")
        assert row["home_team"] == "主隊甲"
        assert row["away_team"] == "客隊乙"
        assert row["competition_code"] == "EPL"
        assert (row["score_ht_home"], row["score_ht_away"]) == (0, 1)
        assert (row["score_ft_home"], row["score_ft_away"]) == (2, 1)
        assert (row["score_2h_home"], row["score_2h_away"]) == (2, 0)
        assert row["result_fetched_at"] == stamp1
        assert row["odds_fetched_at"] is None
        assert row["home_odds"] is None
        assert row["result_status"] is None
        n = conn.execute("SELECT count(*) AS c FROM soccer_daily").fetchone()["c"]
        assert n == 1

    # Same match again: update, no second row
    payload2 = _payload(
        [
            {
                "id": "hist-1",
                "status": "MATCHENDED",
                "frontEndId": "FB1001",
                "kickOffTime": "2015-11-04T20:00:00.000+08:00",
                "homeTeam": {"name_ch": "主隊甲"},
                "awayTeam": {"name_ch": "客隊乙"},
                "tournament": {"code": "EPL", "name_ch": "英超"},
                "results": [
                    _result_row(home=0, away=1, stage_id=3, sequence=1),
                    _result_row(home=3, away=1, stage_id=5, sequence=9),
                ],
            }
        ],
        total=1,
    )
    stamp2 = "2026-11-05T07:00:00+00:00"
    r2 = ingest_official_match_results(
        db, payload2, now_utc=stamp2, variables={"endIndex": 0}, allow_insert=True
    )
    assert r2.inserted == 0
    assert r2.updated == 1
    with get_connection(db) as conn:
        row = conn.execute(
            "SELECT score_ft_home, score_2h_home, result_fetched_at, home_team FROM soccer_daily "
            "WHERE match_id='hist-1';"
        ).fetchone()
        assert row["score_ft_home"] == 3
        assert row["score_2h_home"] == 3
        assert row["result_fetched_at"] == stamp2
        assert row["home_team"] == "主隊甲"  # basic field not overwritten on update
        assert conn.execute("SELECT count(*) AS c FROM soccer_daily").fetchone()["c"] == 1


def test_allow_insert_false_still_skips_unknown(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    payload = _payload(
        [
            {
                "id": "unknown-x",
                "status": "MATCHENDED",
                "frontEndId": "FB9",
                "kickOffTime": "2015-11-04T20:00:00.000+08:00",
                "homeTeam": {"name_ch": "主"},
                "awayTeam": {"name_ch": "客"},
                "results": [
                    _result_row(home=1, away=0, stage_id=3, sequence=1),
                    _result_row(home=1, away=0, stage_id=5, sequence=2),
                ],
            }
        ],
        total=1,
    )
    r = ingest_official_match_results(db, payload, variables={"endIndex": 0}, allow_insert=False)
    assert r.code == "skipped_no_existing"
    assert r.skipped_missing == 1
    assert r.inserted == 0
    with get_connection(db) as conn:
        assert conn.execute("SELECT count(*) AS c FROM soccer_daily").fetchone()["c"] == 0


def test_insert_skips_when_kickoff_missing(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    payload = _payload(
        [
            {
                "id": "no-kick",
                "status": "MATCHENDED",
                "frontEndId": "",
                "homeTeam": {"name_ch": "主"},
                "awayTeam": {"name_ch": "客"},
                "results": [
                    _result_row(home=0, away=0, stage_id=3, sequence=1),
                    _result_row(home=0, away=0, stage_id=5, sequence=2),
                ],
            }
        ],
        total=1,
    )
    r = ingest_official_match_results(db, payload, variables={"endIndex": 0}, allow_insert=True)
    assert r.code == "skipped_no_existing"
    assert r.skipped_insufficient == 1
    assert "kickOffTime" in r.detail
    assert r.inserted == 0


def test_blank_frontend_id_inserts_with_null_match_code(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    payload = _payload(
        [
            {
                "id": "old-1",
                "status": "MATCHENDED",
                "frontEndId": "",
                "kickOffTime": "2005-01-06T20:00:00.000+08:00",
                "homeTeam": {"name_en": "Only EN Home"},
                "awayTeam": {"name_en": "Only EN Away"},
                "results": [
                    _result_row(home=1, away=1, stage_id=3, sequence=1),
                    _result_row(home=1, away=1, stage_id=5, sequence=2),
                ],
            }
        ],
        total=1,
    )
    r = ingest_official_match_results(db, payload, variables={"endIndex": 0}, allow_insert=True)
    assert r.code == "success"
    assert r.inserted == 1
    with get_connection(db) as conn:
        row = conn.execute(
            "SELECT match_code, home_team, away_team FROM soccer_daily WHERE match_id='old-1';"
        ).fetchone()
        assert row["match_code"] is None
        assert row["home_team"] == "Only EN Home"
        assert row["away_team"] == "Only EN Away"


def test_endindex0_incomplete_vs_total_fails(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    _seed_match(db)
    payload = _payload(
        [
            {
                "id": "50079013",
                "status": "MATCHENDED",
                "results": [
                    _result_row(home=0, away=1, stage_id=3, sequence=1),
                    _result_row(home=1, away=1, stage_id=5, sequence=2),
                ],
            }
        ],
        total=65,
    )
    with pytest.raises(SoccerIngestError) as exc:
        ingest_official_match_results(
            db, payload, variables={"startIndex": 0, "endIndex": 0}
        )
    assert exc.value.code == "incomplete_results"


def test_preset_file_loads() -> None:
    from hppv.config import default_config
    from hppv.soccer_requests import load_soccer_request_preset

    cfg = default_config()
    payload, _display = load_soccer_request_preset(cfg.soccer_requests_dir, "match_results")
    assert payload.operation_name == "matchResults"
    assert "matches: matchResult(" in payload.query
    assert payload.variables is not None
    assert payload.variables.get("endIndex") == 0
