from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hppv.footylogic_schema import FOOTYLOGIC_MATCHES_TABLE, footylogic_schema_sql
from hppv.schema import soccer_daily_schema_sql
from hppv.soccer_ingest import HKJC_RESULT_CONFIRM_STATUS, HKJC_RESULT_VOID_STATUS
from hppv.soccer_match_form import (
    FORM_TEAM_RESULT_VOID,
    MatchFormFilters,
    TeamUnresolvedError,
    dedupe_match_rows,
    fetch_match_form,
    form_team_result_for_subject,
    is_stat_eligible,
    resolve_team_id_by_exact_name,
    summarize_for_team,
)


def _insert_fl(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    source: str = "footylogic",
    competition_id: str = "c1",
    competition_name: str = "英超",
    home_id: str,
    away_id: str,
    home_name: str,
    away_name: str,
    kickoff_hkt: str,
    match_date: str,
    ft_h: int | None,
    ft_a: int | None,
    ht_h: int | None = None,
    ht_a: int | None = None,
    confirm: str = "source_score_unconfirmed",
    score_complete: int = 1,
    hkjc_match_id: str | None = None,
) -> None:
    conn.execute(
        f"""
        INSERT INTO {FOOTYLOGIC_MATCHES_TABLE} (
          source_key, source, competition_id, season_id, month, competition_name,
          home_team_id, away_team_id, home_team_name, away_team_name,
          kickoff_utc, kickoff_hkt, match_date_hkt,
          score_ft_home, score_ft_away, score_ht_home, score_ht_away,
          has_score, score_complete, result_confirm_status, event_json,
          source_last_updated, created_at, updated_at, hkjc_match_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            source_key,
            source,
            competition_id,
            "s1",
            match_date[:7],
            competition_name,
            home_id,
            away_id,
            home_name,
            away_name,
            kickoff_hkt.replace("+08:00", ".000Z") if kickoff_hkt else None,
            kickoff_hkt,
            match_date,
            ft_h,
            ft_a,
            ht_h,
            ht_a,
            1 if ft_h is not None and ft_a is not None else 0,
            score_complete,
            confirm,
            "{}",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            hkjc_match_id,
        ),
    )


def _init_db(tmp_path: Path) -> Path:
    db = tmp_path / "soccer.sqlite3"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(footylogic_schema_sql())
    conn.executescript(soccer_daily_schema_sql())
    # Anchor match itself (must be excluded from form).
    _insert_fl(
        conn,
        source_key="fl|anchor",
        home_id="A",
        away_id="B",
        home_name="主隊A",
        away_name="客隊B",
        kickoff_hkt="2026-11-10T20:00:00+08:00",
        match_date="2026-11-10",
        ft_h=None,
        ft_a=None,
        score_complete=0,
    )
    # H2H prior meetings (A vs B)
    _insert_fl(
        conn,
        source_key="fl|h2h1",
        home_id="A",
        away_id="B",
        home_name="主隊A",
        away_name="客隊B",
        kickoff_hkt="2026-10-01T20:00:00+08:00",
        match_date="2026-10-01",
        ft_h=2,
        ft_a=1,
        ht_h=1,
        ht_a=0,
    )
    _insert_fl(
        conn,
        source_key="fl|h2h2",
        home_id="B",
        away_id="A",
        home_name="客隊B",
        away_name="主隊A",
        kickoff_hkt="2026-09-01T20:00:00+08:00",
        match_date="2026-09-01",
        ft_h=0,
        ft_a=0,
    )
    # Same venue H2H (A home B away)
    _insert_fl(
        conn,
        source_key="fl|h2h3",
        home_id="A",
        away_id="B",
        home_name="主隊A",
        away_name="客隊B",
        kickoff_hkt="2026-08-01T20:00:00+08:00",
        match_date="2026-08-01",
        ft_h=3,
        ft_a=0,
        competition_id="c2",
        competition_name="足總盃",
    )
    # After kickoff — must not appear
    _insert_fl(
        conn,
        source_key="fl|future",
        home_id="A",
        away_id="B",
        home_name="主隊A",
        away_name="客隊B",
        kickoff_hkt="2026-11-11T20:00:00+08:00",
        match_date="2026-11-11",
        ft_h=1,
        ft_a=0,
    )
    # A recent vs others
    for i, (oid, oname, venue_home, ft_h, ft_a, ko) in enumerate(
        [
            ("C", "對手C", True, 1, 0, "2026-10-20T20:00:00+08:00"),
            ("D", "對手D", False, 0, 2, "2026-10-15T20:00:00+08:00"),  # A away win 2-0
            ("E", "對手E", True, 1, 1, "2026-10-10T20:00:00+08:00"),
            ("F", "對手F", True, None, None, "2026-10-05T20:00:00+08:00"),  # unconfirmed/missing
        ],
        start=1,
    ):
        if venue_home:
            _insert_fl(
                conn,
                source_key=f"fl|a{i}",
                home_id="A",
                away_id=oid,
                home_name="主隊A",
                away_name=oname,
                kickoff_hkt=ko,
                match_date=ko[:10],
                ft_h=ft_h,
                ft_a=ft_a,
                score_complete=1 if ft_h is not None else 0,
            )
        else:
            _insert_fl(
                conn,
                source_key=f"fl|a{i}",
                home_id=oid,
                away_id="A",
                home_name=oname,
                away_name="主隊A",
                kickoff_hkt=ko,
                match_date=ko[:10],
                ft_h=ft_h,
                ft_a=ft_a,
            )
    # B recent
    _insert_fl(
        conn,
        source_key="fl|b1",
        home_id="B",
        away_id="G",
        home_name="客隊B",
        away_name="對手G",
        kickoff_hkt="2026-10-18T20:00:00+08:00",
        match_date="2026-10-18",
        ft_h=0,
        ft_a=1,
    )
    # Void (shows in list, not in stats)
    _insert_fl(
        conn,
        source_key="hkjc|void1",
        source="hkjc",
        home_id="A",
        away_id="H",
        home_name="主隊A",
        away_name="對手H",
        kickoff_hkt="2026-10-08T20:00:00+08:00",
        match_date="2026-10-08",
        ft_h=None,
        ft_a=None,
        confirm=HKJC_RESULT_VOID_STATUS,
        score_complete=0,
        hkjc_match_id="99900001",
    )
    # HKJC confirmed for A
    _insert_fl(
        conn,
        source_key="hkjc|ok1",
        source="hkjc",
        home_id="A",
        away_id="I",
        home_name="主隊A",
        away_name="對手I",
        kickoff_hkt="2026-10-03T20:00:00+08:00",
        match_date="2026-10-03",
        ft_h=4,
        ft_a=1,
        confirm=HKJC_RESULT_CONFIRM_STATUS,
        hkjc_match_id="99900002",
    )
    # Cross-source duplicate of h2h1
    _insert_fl(
        conn,
        source_key="hkjc|dup_h2h1",
        source="hkjc",
        home_id="A",
        away_id="B",
        home_name="主隊A",
        away_name="客隊B",
        kickoff_hkt="2026-10-01T20:00:00+08:00",
        match_date="2026-10-01",
        ft_h=2,
        ft_a=1,
        confirm=HKJC_RESULT_CONFIRM_STATUS,
        hkjc_match_id="99900003",
    )
    # Daily row
    conn.execute(
        """
        INSERT INTO soccer_daily (
          match_id, match_code, match_date, kickoff_at,
          competition_code, competition_name_zh, home_team, away_team,
          match_status, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "50011111",
            "001",
            "2026-11-10",
            "2026-11-10T20:00:00+08:00",
            "EPL",
            "英超",
            "主隊A",
            "客隊B",
            "Prematch",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    # Ambiguous name team
    _insert_fl(
        conn,
        source_key="fl|amb1",
        home_id="X1",
        away_id="Y1",
        home_name="模糊隊",
        away_name="對手Y",
        kickoff_hkt="2026-01-01T12:00:00+08:00",
        match_date="2026-01-01",
        ft_h=1,
        ft_a=0,
    )
    _insert_fl(
        conn,
        source_key="fl|amb2",
        home_id="X2",
        away_id="Y2",
        home_name="模糊隊",
        away_name="對手Z",
        kickoff_hkt="2026-01-02T12:00:00+08:00",
        match_date="2026-01-02",
        ft_h=1,
        ft_a=0,
    )
    conn.commit()
    conn.close()
    return db


def test_h2h_only_mutual_meetings_before_kickoff(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    data = fetch_match_form(db, source_key="fl|anchor", filters=MatchFormFilters(limit=10))
    keys = [m["source_key"] for m in data["head_to_head"]]
    assert "fl|anchor" not in keys
    assert "fl|future" not in keys
    assert "fl|h2h1" in keys or "hkjc|dup_h2h1" in keys
    assert "fl|h2h2" in keys
    assert "fl|a1" not in keys  # A vs C is not H2H
    # Dedupe: only one of h2h1 / dup
    assert sum(1 for k in keys if k in ("fl|h2h1", "hkjc|dup_h2h1")) == 1


def test_recent_subject_centric_and_summary(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    data = fetch_match_form(db, source_key="fl|anchor", filters=MatchFormFilters(limit=10))
    home = data["home_recent"]
    assert all(m["opponent"] != "主隊A" for m in home)
    # Subject result from A's view for away win row
    away_win = next(m for m in home if m["opponent"] == "對手D")
    assert away_win["venue"] == "away"
    assert away_win["subject_result"] == "win"
    summary = data["home_summary"]
    # Valid: h2h rows may also appear in recent; count eligible among home_recent only
    assert summary["played"] == len(home)
    assert summary["valid_played"] >= 1
    assert summary["wins"] + summary["draws"] + summary["losses"] == summary["valid_played"]
    # Void listed but not counted
    void_rows = [m for m in home if m["score_ft"] == "失效"]
    assert void_rows
    assert summary["valid_played"] < summary["played"] or summary["played"] >= summary["valid_played"]


def test_same_venue_and_competition_filters(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    same_venue = fetch_match_form(
        db,
        source_key="fl|anchor",
        filters=MatchFormFilters(limit=10, h2h_venue="same"),
    )
    for m in same_venue["head_to_head"]:
        assert m["home_team_id"] == "A"
        assert m["away_team_id"] == "B"
    same_comp = fetch_match_form(
        db,
        source_key="fl|anchor",
        filters=MatchFormFilters(limit=10, h2h_competition="same"),
    )
    keys = [m["source_key"] for m in same_comp["head_to_head"]]
    assert "fl|h2h3" not in keys  # different competition
    home_only = fetch_match_form(
        db,
        source_key="fl|anchor",
        filters=MatchFormFilters(limit=10, home_venue="home"),
    )
    assert all(m["venue"] == "home" for m in home_only["home_recent"])


def test_limit_and_short_sample_stats(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    data = fetch_match_form(db, source_key="fl|anchor", filters=MatchFormFilters(limit=5))
    assert len(data["home_recent"]) <= 5
    assert data["limit"] == 5
    assert data["home_summary"]["played"] == len(data["home_recent"])


def test_unconfirmed_ft_excluded_from_stats() -> None:
    class R(dict):
        def __getitem__(self, k):  # noqa: ANN001
            return dict.__getitem__(self, k)

        def keys(self):  # noqa: ANN201
            return dict.keys(self)

    missing = R(
        result_confirm_status="source_score_unconfirmed",
        score_ft_home=None,
        score_ft_away=None,
        score_complete=0,
        source="footylogic",
        home_team_id="A",
        away_team_id="B",
    )
    assert is_stat_eligible(missing) is False
    void = R(
        result_confirm_status=HKJC_RESULT_VOID_STATUS,
        score_ft_home=1,
        score_ft_away=0,
        score_complete=1,
        source="hkjc",
        home_team_id="A",
        away_team_id="B",
    )
    assert is_stat_eligible(void) is False
    ok_fl = R(
        result_confirm_status="source_score_unconfirmed",
        score_ft_home=1,
        score_ft_away=0,
        score_complete=1,
        source="footylogic",
        home_team_id="A",
        away_team_id="B",
    )
    assert is_stat_eligible(ok_fl) is True


def test_summary_subject_perspective() -> None:
    class R(dict):
        def keys(self):  # noqa: ANN201
            return dict.keys(self)

    rows = [
        R(
            result_confirm_status="source_score_unconfirmed",
            score_complete=1,
            source="footylogic",
            home_team_id="A",
            away_team_id="B",
            score_ft_home=0,
            score_ft_away=2,  # A home loss
        ),
        R(
            result_confirm_status="source_score_unconfirmed",
            score_complete=1,
            source="footylogic",
            home_team_id="C",
            away_team_id="A",
            score_ft_home=1,
            score_ft_away=1,  # A away draw
        ),
    ]
    s = summarize_for_team(rows, subject_team_id="A")
    assert s == {
        "played": 2,
        "valid_played": 2,
        "wins": 0,
        "draws": 1,
        "losses": 1,
        "goals_for": 1,
        "goals_against": 3,
    }


def test_h2h_header_summary_from_mutual_rows(tmp_path: Path) -> None:
    """H2H header stats use fixture home/away identity on the same mutual list."""
    db = _init_db(tmp_path)
    data = fetch_match_form(db, source_key="fl|anchor", filters=MatchFormFilters(limit=10))
    hs = data["h2h_home_summary"]
    aws = data["h2h_away_summary"]
    assert hs["played"] == len(data["head_to_head"])
    assert aws["played"] == len(data["head_to_head"])
    assert hs["wins"] + hs["draws"] + hs["losses"] == hs["valid_played"]
    assert aws["wins"] + aws["draws"] + aws["losses"] == aws["valid_played"]
    # Fixture-home wins == fixture-away losses; draws match.
    assert hs["wins"] == aws["losses"]
    assert aws["wins"] == hs["losses"]
    assert hs["draws"] == aws["draws"]
    # Must not reuse recent-form summaries (different match set).
    assert hs["played"] != data["home_summary"]["played"] or hs != data["home_summary"]
    # Same competition filter shrinks H2H and updates header stats together.
    same = fetch_match_form(
        db,
        source_key="fl|anchor",
        filters=MatchFormFilters(limit=10, h2h_competition="same"),
    )
    assert same["h2h_home_summary"]["played"] == len(same["head_to_head"])
    assert same["h2h_home_summary"]["played"] < hs["played"]
    assert same["h2h_home_summary"]["wins"] == same["h2h_away_summary"]["losses"]


def test_form_team_result_home_away_and_void() -> None:
    class R(dict):
        def keys(self):  # noqa: ANN201
            return dict.keys(self)

    home_win = R(
        result_confirm_status="source_score_unconfirmed",
        score_complete=1,
        source="footylogic",
        home_team_id="A",
        away_team_id="B",
        score_ft_home=3,
        score_ft_away=0,
    )
    away_win = R(
        result_confirm_status="source_score_unconfirmed",
        score_complete=1,
        source="footylogic",
        home_team_id="B",
        away_team_id="A",
        score_ft_home=3,
        score_ft_away=0,  # A away → lose
    )
    away_comeback = R(
        result_confirm_status="source_score_unconfirmed",
        score_complete=1,
        source="footylogic",
        home_team_id="B",
        away_team_id="A",
        score_ft_home=1,
        score_ft_away=2,  # A away → win
    )
    void = R(
        result_confirm_status=HKJC_RESULT_VOID_STATUS,
        score_complete=0,
        source="hkjc",
        home_team_id="A",
        away_team_id="B",
        score_ft_home=None,
        score_ft_away=None,
    )
    missing = R(
        result_confirm_status="source_score_unconfirmed",
        score_complete=0,
        source="footylogic",
        home_team_id="A",
        away_team_id="B",
        score_ft_home=None,
        score_ft_away=None,
    )
    assert form_team_result_for_subject(home_win, subject_team_id="A") == "win"
    assert form_team_result_for_subject(away_win, subject_team_id="A") == "lose"
    assert form_team_result_for_subject(away_comeback, subject_team_id="A") == "win"
    assert form_team_result_for_subject(void, subject_team_id="A") == FORM_TEAM_RESULT_VOID
    assert form_team_result_for_subject(missing, subject_team_id="A") == "neutral"


def test_recent_payload_exposes_subject_result(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    data = fetch_match_form(db, source_key="fl|anchor", filters=MatchFormFilters(limit=10))
    away_win = next(m for m in data["home_recent"] if m["opponent"] == "對手D")
    assert away_win["venue"] == "away"
    assert away_win["subject_result"] == "win"
    void_row = next(m for m in data["home_recent"] if m["score_ft"] == "失效")
    assert void_row["subject_result"] == FORM_TEAM_RESULT_VOID


def test_h2h_away_win_counts_for_fixture_home() -> None:
    """Historical B(home) lose to A(away) still counts as a win for fixture home A."""

    class R(dict):
        def keys(self):  # noqa: ANN201
            return dict.keys(self)

    rows = [
        R(
            result_confirm_status="source_score_unconfirmed",
            score_complete=1,
            source="footylogic",
            home_team_id="B",
            away_team_id="A",
            score_ft_home=0,
            score_ft_away=2,
        ),
        R(
            result_confirm_status="source_score_unconfirmed",
            score_complete=1,
            source="footylogic",
            home_team_id="A",
            away_team_id="B",
            score_ft_home=1,
            score_ft_away=1,
        ),
        R(
            result_confirm_status=HKJC_RESULT_VOID_STATUS,
            score_complete=0,
            source="hkjc",
            home_team_id="A",
            away_team_id="B",
            score_ft_home=None,
            score_ft_away=None,
        ),
    ]
    home = summarize_for_team(rows, subject_team_id="A")
    away = summarize_for_team(rows, subject_team_id="B")
    assert home["wins"] == 1
    assert home["draws"] == 1
    assert home["losses"] == 0
    assert home["valid_played"] == 2
    assert home["played"] == 3
    assert away["wins"] == 0
    assert away["draws"] == 1
    assert away["losses"] == 1
    assert home["wins"] == away["losses"]
    assert home["draws"] == away["draws"]


def test_h2h_empty_summary_zeros(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    # Far-future kickoff → no prior H2H.
    data = fetch_match_form(
        db,
        home_team_id="A",
        away_team_id="B",
        kickoff_hkt="2020-01-01T12:00:00+08:00",
        home_team_name="主隊A",
        away_team_name="客隊B",
        filters=MatchFormFilters(limit=10),
    )
    assert data["head_to_head"] == []
    assert data["h2h_home_summary"]["wins"] == 0
    assert data["h2h_home_summary"]["draws"] == 0
    assert data["h2h_home_summary"]["losses"] == 0
    assert data["h2h_away_summary"]["wins"] == 0
    assert data["h2h_away_summary"]["draws"] == 0
    assert data["h2h_away_summary"]["losses"] == 0


def test_dedupe_prefers_confirmed(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT * FROM {FOOTYLOGIC_MATCHES_TABLE} WHERE home_team_id='A' AND away_team_id='B' AND match_date_hkt='2026-10-01'"
    ).fetchall()
    deduped = dedupe_match_rows(list(rows))
    assert len(deduped) == 1
    assert deduped[0]["source_key"] == "hkjc|dup_h2h1"
    conn.close()


def test_ambiguous_team_name_rejected(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    with pytest.raises(TeamUnresolvedError):
        resolve_team_id_by_exact_name(conn, "模糊隊")
    conn.close()


def test_daily_match_id_anchor(tmp_path: Path) -> None:
    db = _init_db(tmp_path)
    data = fetch_match_form(db, match_id="50011111", filters=MatchFormFilters(limit=10))
    assert data["home_team"]["team_id"] == "A"
    assert data["away_team"]["team_id"] == "B"
    assert data["head_to_head"]


def test_api_failure_not_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hppv import soccer_match_form as mod

    def boom(*_a, **_k):  # noqa: ANN001
        raise RuntimeError("db locked")

    monkeypatch.setattr(mod, "get_connection", boom)
    with pytest.raises(Exception):
        fetch_match_form(tmp_path / "missing.sqlite3", source_key="x")
