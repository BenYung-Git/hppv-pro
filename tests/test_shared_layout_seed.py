"""Layout first-use seed: missing storage → catalog persist → require succeeds."""

from __future__ import annotations

from pathlib import Path

import pytest

from hppv.schema import soccer_layout_settings_schema_sql
from hppv.soccer_db import get_connection
from hppv.shared_layout import (
    LAYOUT_KEY_DAILY_MATCHES,
    LAYOUT_KEY_MATCH_HISTORY,
    LAYOUT_KEY_RACING_DAILY,
    LAYOUT_KEY_RACING_HISTORY,
    SPORT_RACING,
    SPORT_SOCCER,
    SharedLayoutError,
    ensure_all_layout_settings,
    layout_settings_from_catalog,
    require_layout_settings,
)
import hppv.racing_layout as rl


def _minimal_soccer_db(path: Path) -> Path:
    """Layout-table-only DB (avoid full init_soccer_db / tzdata dependency)."""
    with get_connection(path) as conn:
        conn.executescript(soccer_layout_settings_schema_sql())
        # History catalog may PRAGMA footylogic_matches — empty table is enough.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS footylogic_matches (match_id TEXT PRIMARY KEY);"
        )
        conn.commit()
    return path


def test_racing_require_seeds_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "racing_layout_settings.json"
    monkeypatch.setattr(rl, "layout_settings_path", lambda: path)
    assert not path.exists()

    daily = require_layout_settings(SPORT_RACING, LAYOUT_KEY_RACING_DAILY)
    hist = require_layout_settings(SPORT_RACING, LAYOUT_KEY_RACING_HISTORY)

    assert path.exists()
    assert daily["columns"]
    assert hist["columns"]
    assert all(c["visible"] for c in daily["columns"])
    assert daily["zebra_color_id"] == "row_alt_gray"

    # Second call does not rewrite when already valid (same content shape).
    again = require_layout_settings(SPORT_RACING, LAYOUT_KEY_RACING_DAILY)
    assert len(again["columns"]) == len(daily["columns"])


def test_soccer_require_seeds_missing_rows(tmp_path: Path) -> None:
    db = _minimal_soccer_db(tmp_path / "soccer.sqlite3")
    with get_connection(db) as conn:
        n = conn.execute("SELECT COUNT(*) c FROM soccer_layout_settings").fetchone()["c"]
    assert n == 0

    daily = require_layout_settings(
        SPORT_SOCCER, LAYOUT_KEY_DAILY_MATCHES, soccer_db_path=db
    )
    hist = require_layout_settings(
        SPORT_SOCCER, LAYOUT_KEY_MATCH_HISTORY, soccer_db_path=db
    )
    assert daily["columns"]
    assert hist["columns"]
    with get_connection(db) as conn:
        keys = {
            r["layout_key"]
            for r in conn.execute("SELECT layout_key FROM soccer_layout_settings")
        }
    assert keys == {LAYOUT_KEY_DAILY_MATCHES, LAYOUT_KEY_MATCH_HISTORY}


def test_ensure_all_layout_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "racing_layout_settings.json"
    monkeypatch.setattr(rl, "layout_settings_path", lambda: path)
    db = _minimal_soccer_db(tmp_path / "soccer.sqlite3")

    out = ensure_all_layout_settings(soccer_db_path=db)
    assert f"{SPORT_RACING}:{LAYOUT_KEY_RACING_DAILY}" in out
    assert f"{SPORT_RACING}:{LAYOUT_KEY_RACING_HISTORY}" in out
    assert f"{SPORT_SOCCER}:{LAYOUT_KEY_DAILY_MATCHES}" in out
    assert f"{SPORT_SOCCER}:{LAYOUT_KEY_MATCH_HISTORY}" in out
    assert path.exists()


def test_invalid_racing_json_not_silently_seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "racing_layout_settings.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(rl, "layout_settings_path", lambda: path)
    with pytest.raises(SharedLayoutError) as exc:
        require_layout_settings(SPORT_RACING, LAYOUT_KEY_RACING_DAILY)
    assert exc.value.code == "invalid_stored_layout"


def test_layout_settings_from_catalog_matches_racing_defaults() -> None:
    seeded = layout_settings_from_catalog(SPORT_RACING, LAYOUT_KEY_RACING_HISTORY)
    assert len(seeded["columns"]) == len(rl.RACING_HISTORY_COLUMN_CATALOG)
    assert seeded["zebra_enabled"] is True
