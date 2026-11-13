"""Daily vs history URL scope isolation + history-first daily load flag."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from hppv.soccer_db import init_soccer_db
from hppv.soccer_matches import hong_kong_today
from hppv.web_app import _resolve_soccer_tab_scopes


def test_resolve_scopes_legacy_history_does_not_pollute_daily() -> None:
    daily, hist = _resolve_soccer_tab_scopes(
        {"tab": "history", "all_matches": "0", "date": "2026-11-12"},
        active_tab="history",
    )
    assert hist["all_matches"] is False
    assert hist["selected"] == date(2026, 11, 12)
    assert hist["compat_legacy"] is True
    assert daily["all_matches"] is True
    assert daily["selected"] == hong_kong_today()
    assert daily["compat_legacy"] is False


def test_resolve_scopes_prefixed_keeps_both() -> None:
    daily, hist = _resolve_soccer_tab_scopes(
        {
            "tab": "matches",
            "all_matches": "1",
            "date": "2026-11-13",
            "history_all_matches": "0",
            "history_date": "2026-11-12",
        },
        active_tab="matches",
    )
    assert daily["all_matches"] is True
    assert hist["all_matches"] is False
    assert hist["selected"] == date(2026, 11, 12)


def test_history_tab_skips_daily_load_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    from hppv import web_app as wa
    from hppv.soccer_db import get_connection

    with get_connection(db) as conn:
        conn.execute(
            """
            INSERT INTO soccer_daily (
              match_id, match_code, match_date, kickoff_at,
              competition_code, competition_name_zh, home_team, away_team,
              match_status, created_at, updated_at
            ) VALUES (
              'D1', 'FB1', '2026-11-13', '2026-11-13T12:00:00+08:00',
              'EPL', '英超', '主', '客', 'PREEVENT', 't', 't'
            )
            """
        )
        conn.commit()
    monkeypatch.setattr(wa, "SOCCER_DB_PATH", db)
    client = wa.app.test_client()
    html = client.get("/soccer/daily?tab=history&all_matches=0&date=2026-11-12").get_data(
        as_text=True
    )
    assert 'id="matches-pending-box"' in html or "載入每日賽事" in html or "Loading daily" in html
    assert '"loaded": false' in html or '"loaded":false' in html
    # Daily date chrome must not be forced to history's 2026-11-12 when defaults apply.
    # Isolated daily defaults to all_matches=true; date input still has a value.
    assert 'id="match-all-matches"' in html
