"""Draw statistics parse / upsert / list unit tests."""

from __future__ import annotations

from pathlib import Path

from hppv.racing_draw_statistics import (
    DrawStatisticsRow,
    ensure_draw_statistics_table,
    fetch_draw_statistics_filtered,
    parse_drawadvsearch_result_html,
    upsert_draw_statistics_rows,
)
from hppv.soccer_db import init_soccer_db
from hppv.translations import load_active_text_map, translate

SAMPLE_HTML = """
<html><body>
<table class="table_bd">
<tr><td>26/27 馬季至今 沙田 1600米 草地 "C+3" 賽道</td></tr>
<tr>
  <td>檔位</td><td>出賽 次數</td><td>冠</td><td>亞</td><td>季</td><td>殿</td>
  <td>勝出率 %</td><td>入Q率 %</td><td>上名率 %</td><td>前4名 率%</td><td></td>
</tr>
<tr><td>1</td><td>45</td><td>3</td><td>7</td><td>5</td><td>4</td><td>7</td><td>22</td><td>33</td><td>42</td><td></td></tr>
<tr><td>2</td><td>46</td><td>7</td><td>4</td><td>6</td><td>3</td><td>15</td><td>24</td><td>37</td><td>43</td><td></td></tr>
<tr><td colspan="11">大熱門 : 勝出率 20%</td></tr>
</table>
</body></html>
"""


def test_parse_empty_hkjc_message() -> None:
    rows = parse_drawadvsearch_result_html(
        "<html><body><table><tr><td>找不到相關資料</td></tr></table></body></html>",
        venue_code="ST",
        track_type="TURF",
        rail_position="A",
        distance_m=2200,
        source_url="https://example.test/empty",
        fetched_at="2026-01-01T00:00:00+00:00",
    )
    assert rows == []


def test_parse_and_derived_counts() -> None:
    rows = parse_drawadvsearch_result_html(
        SAMPLE_HTML,
        venue_code="ST",
        track_type="TURF",
        rail_position="C+3",
        distance_m=1600,
        source_url="https://example.test/draw",
        fetched_at="2026-01-01T00:00:00+00:00",
    )
    assert len(rows) == 2
    r0 = rows[0]
    assert r0.draw_no == 1
    assert r0.starts == 45
    assert r0.wins == 3
    assert r0.win_count == 3
    assert r0.in_place_count == 7 + 5 + 4
    assert r0.lose_count == 45 - 3 - 7 - 5 - 4
    assert r0.win_pct == "7"
    assert r0.top4_pct == "42"


def test_upsert_and_list(tmp_path: Path) -> None:
    rows = parse_drawadvsearch_result_html(
        SAMPLE_HTML,
        venue_code="HV",
        track_type="TURF",
        rail_position="C+3",
        distance_m=1650,
        source_url="https://example.test/draw",
        fetched_at="2026-01-01T00:00:00+00:00",
    )
    db = tmp_path / "draw.sqlite3"
    ensure_draw_statistics_table(db)
    c1 = upsert_draw_statistics_rows(db, rows, overwrite=False)
    assert c1["inserted"] == 2
    c2 = upsert_draw_statistics_rows(db, rows, overwrite=False)
    assert c2["skipped"] == 2
    changed = [
        DrawStatisticsRow(
            venue_code="HV",
            track_type="TURF",
            rail_position="C+3",
            distance_m=1650,
            draw_no=1,
            starts=50,
            wins=4,
            seconds=7,
            thirds=5,
            fourths=4,
            win_pct="8",
            quinella_pct="22",
            place_pct="32",
            top4_pct="40",
            source="drawStatisticsAdvSearch",
            source_url="https://example.test/draw2",
            fetched_at="2026-01-02T00:00:00+00:00",
        )
    ]
    c3 = upsert_draw_statistics_rows(db, changed, overwrite=True)
    assert c3["updated"] == 1
    result = fetch_draw_statistics_filtered(
        db, venue_code="HV", rail_position="C+3", n=1
    )
    assert result.total_count == 2
    one = next(r for r in result.rows if r["draw_no"] == "1")
    assert one["starts"] == "50"
    assert one["win_count"] == "4"
    assert one["in_place_count"] == "16"
    assert one["lose_count"] == "30"


def test_draw_statistics_translation_keys(tmp_path: Path) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    assert (
        translate(load_active_text_map(db, "zh-Hant"), "racing.info.tab.draw_statistics")
        == "檔位統計"
    )
    assert (
        translate(
            load_active_text_map(db, "en"), "racing.download.type.draw_statistics"
        )
        == "Draw statistics"
    )
    assert (
        translate(
            load_active_text_map(db, "zh-Hant"), "racing.task.update_draw_statistics"
        )
        == "檔位統計"
    )
