"""Racing info UI shell: nav, empty tabs, i18n — no business logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from hppv.soccer_db import init_soccer_db
from hppv.translations import load_active_text_map, translate


def test_racing_translation_keys_seeded(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    assert translate(load_active_text_map(db, "zh-Hant"), "nav.racing_info") == "賽馬資訊"
    assert translate(load_active_text_map(db, "en"), "nav.racing_info") == "Horse Racing"
    assert translate(load_active_text_map(db, "zh-Hans"), "nav.racing_info") == "赛马资讯"
    assert translate(load_active_text_map(db, "en"), "racing.info.tab.matches") == "Daily Races"
    assert translate(load_active_text_map(db, "en"), "racing.info.tab.history") == "Racing History"
    assert (
        translate(load_active_text_map(db, "zh-Hant"), "racing.info.tab.standard_times")
        == "標準時間"
    )
    assert translate(load_active_text_map(db, "zh-Hans"), "racing.info.tab.download") == "下载资料"
    assert translate(load_active_text_map(db, "zh-Hant"), "racing.history.title") == "賽馬往績"
    assert translate(load_active_text_map(db, "en"), "racing.history.col.horse_id") == "Horse ID"
    assert translate(load_active_text_map(db, "zh-Hant"), "racing.download.type.daily_results") == "即日賽果"
    assert translate(load_active_text_map(db, "en"), "racing.task.update_daily_results") == "Same-day results"
    assert translate(load_active_text_map(db, "zh-Hant"), "racing.download.type.horses") == "馬匹資料"
    assert translate(load_active_text_map(db, "en"), "racing.download.type.horses") == "Horse data"
    assert translate(load_active_text_map(db, "zh-Hant"), "racing.task.update_horses") == "馬匹資料"
    assert translate(load_active_text_map(db, "en"), "racing.task.update_horses") == "Horse data"
    # Soccer keys unchanged
    assert translate(load_active_text_map(db, "en"), "soccer.daily.tab.matches") == "Daily Matches"
    assert translate(load_active_text_map(db, "en"), "nav.soccer_daily") == "Soccer Info"


def test_racing_info_route_default_and_tabs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hppv import web_app as wa

    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    monkeypatch.setattr(wa, "SOCCER_DB_PATH", db)
    client = wa.app.test_client()

    r = client.get("/racing/info")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'data-racing-shell="1"' in html
    assert "racing-tab-btn-matches" in html
    assert "racing-tab-panel-matches" in html
    assert 'id="racing-tab-btn-matches"' in html
    assert 'class="settings-tab active"' in html
    # Empty shell tabs (non-history): no loading / empty-data copy
    assert "/api/soccer/" not in html

    for tab in ("download", "tasks"):
        rt = client.get(f"/racing/info?tab={tab}")
        assert rt.status_code == 200
        body = rt.get_data(as_text=True)
        assert f'data-tab="{tab}"' in body
        assert f'id="racing-tab-panel-{tab}"' in body
        # Empty shell panels stay blank (history panel may still exist elsewhere in DOM).
        start = body.find(f'id="racing-tab-panel-{tab}"')
        end = body.find("tab-panel", start + 10)
        panel = body[start:end] if start != -1 else ""
        assert "載入中" not in panel
        assert "沒有資料" not in panel
        assert "/api/soccer/" not in panel

    hist = client.get("/racing/info?tab=history")
    assert hist.status_code == 200
    hist_html = hist.get_data(as_text=True)
    assert 'data-tab="history"' in hist_html
    assert "racing-history-table" in hist_html
    assert "/api/records/racing/racing_history" in hist_html

    std = client.get("/racing/info?tab=standard_times")
    assert std.status_code == 200
    std_html = std.get_data(as_text=True)
    assert 'data-tab="standard_times"' in std_html
    assert "racing-std-table" in std_html
    assert "/api/records/racing/racing_standard_times" in std_html

    bad = client.get("/racing/info?tab=nope")
    assert bad.status_code == 200
    assert 'id="racing-tab-btn-matches"' in bad.get_data(as_text=True)


def test_nav_order_racing_left_of_soccer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hppv import web_app as wa

    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    monkeypatch.setattr(wa, "SOCCER_DB_PATH", db)
    client = wa.app.test_client()
    html = client.get("/racing/info").get_data(as_text=True)
    i_racing = html.find('href="/racing/info"')
    i_soccer = html.find('href="/soccer/daily"')
    assert i_racing != -1 and i_soccer != -1
    assert i_racing < i_soccer
    assert 'class="nav-active"' in html

    soccer = client.get("/soccer/daily").get_data(as_text=True)
    assert 'href="/racing/info"' in soccer
    # Soccer page still present
    assert "soccer-daily" in soccer or "tab-btn-matches" in soccer
