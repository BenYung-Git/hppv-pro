"""指定賽果時段：localresults HTML → race_records INSERT OR IGNORE."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from hppv.db import fetch_race_records, get_connection, init_db
from hppv.racing_download import run_results_period_download
from hppv.racing_local_results import (
    RESULTS_PERIOD_QUERY,
    discover_race_nos,
    parse_localresults_race_html,
    parse_results_period_variables,
)
from hppv.racing_requests import (
    REQUEST_KEY_RESULTS_PERIOD,
    RacingRequestPresetError,
    RacingRequestPayload,
    assert_payload_matches_whitelist,
    ensure_racing_preset_files,
    expected_query_for_preset,
    load_racing_request_preset,
)

_SAMPLE_HTML = """
<html><body>
<div>第 1 場 (189)</div>
<div>第四班 - 1200米 - (60-40)</div>
<div>場地狀況 : 好地</div>
<div>賽道 : 草地 - "B" 賽道</div>
<div>HK$ 1,170,000</div>
<a href="?RaceNo=1">1</a><a href="?RaceNo=2">2</a>
<table>
  <tr>
    <td>名次</td><td>馬號</td><td>馬名</td><td>騎師</td><td>練馬師</td>
    <td>實際 負磅</td><td>排位 體重</td><td>檔位</td><td>頭馬 距離</td>
    <td>沿途 走位</td><td>完成 時間</td><td>獨贏 賠率</td>
  </tr>
  <tr>
    <td>1</td><td>12</td>
    <td><a href="/zh-hk/local/information/horse?horseid=HK_2024_K406">知道穩勝 (K406)</a></td>
    <td>班德禮</td><td>沈集成</td><td>117</td><td>1062</td><td>8</td>
    <td>---</td><td>4 4 1</td><td>1:09.68</td><td>8.1</td>
  </tr>
  <tr>
    <td>2</td><td>11</td>
    <td><a href="/zh-hk/local/information/horse?horseid=HK_2024_K025">凌風傲雪 (K025)</a></td>
    <td>金誠剛</td><td>姚本輝</td><td>118</td><td>1111</td><td>7</td>
    <td>1</td><td>1 1 2</td><td>1:09.83</td><td>13</td>
  </tr>
  <tr>
    <td>WV</td><td>3</td>
    <td><a href="/zh-hk/local/information/horse?horseid=HK_2024_K902">風耀奪標 (K902)</a></td>
    <td></td><td>蔡約翰</td><td></td><td></td><td>3</td>
    <td></td><td></td><td></td><td></td>
  </tr>
</table>
<table>
  <tr><td>名次</td><td>馬號</td><td>馬名</td><td>競賽事件</td></tr>
  <tr><td>1</td><td>12</td><td>知道穩勝 (K406)</td><td>無</td></tr>
</table>
</body></html>
"""


def test_parse_localresults_race_html() -> None:
    rows, horses = parse_localresults_race_html(
        _SAMPLE_HTML, race_date=date(2025, 11, 19), venue_code="HV"
    )
    assert len(rows) == 3
    r0 = rows[0]
    assert r0.horse_id == "HK_2024_K406"
    assert r0.race_date == "19/11/25"
    assert r0.venue == "跑馬地"
    assert r0.race_no == "189"
    assert r0.course == '跑馬地草地"B"'
    assert r0.distance == "1200"
    assert r0.going == "好"
    assert r0.class_band == "第四班"
    assert r0.draw == "8"
    assert r0.jockey == "班德禮"
    assert r0.trainer == "沈集成"
    assert r0.finish_pos == "01"
    assert r0.running_pos == "4 4 1"
    assert r0.time == "1:09.68"
    assert r0.win_odds == "8.1"
    assert r0.prize == "1170000"
    assert rows[2].finish_pos == "WV"
    assert rows[2].horse_id == "HK_2024_K902"
    by_id = {h["horse_id"]: h for h in horses}
    assert by_id["HK_2024_K902"]["name_zh"] == "風耀奪標"
    assert by_id["HK_2024_K902"]["brand_no"] == "K902"
    assert rows[0].handicap == "117"
    assert rows[0].running_pos == "4 4 1"
    assert rows[0].time == "1:09.68"
    assert rows[0].win_odds == "8.1"
    assert discover_race_nos(_SAMPLE_HTML) == [1, 2]


def test_parse_results_period_variables() -> None:
    start, end, venues = parse_results_period_variables(
        {
            "startDate": "2025-11-19",
            "endDate": "2025-11-19",
            "venueCodes": ["HV"],
        }
    )
    assert start == date(2025, 11, 19)
    assert end == date(2025, 11, 19)
    assert venues == ["HV"]


def test_results_period_insert_then_ignore(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "hppv.sqlite3"
    init_db(db)

    def fake_collect(*, start, end, venue_codes, **kwargs):
        rows, horses = parse_localresults_race_html(
            _SAMPLE_HTML, race_date=start, venue_code=venue_codes[0]
        )
        meta = [
            {
                "race_date": "19/11/2025",
                "venue_code": "HV",
                "race_nos": [1],
                "races_fetched": 1,
                "races_with_results": 1,
            }
        ]
        return rows, horses, meta

    monkeypatch.setattr(
        "hppv.racing_download.collect_period_race_records", fake_collect
    )

    first = run_results_period_download(
        db,
        variables={
            "startDate": "2025-11-19",
            "endDate": "2025-11-19",
            "venueCodes": ["HV"],
        },
    )
    assert first.code == "success"
    assert first.progress.updated == 3
    assert first.progress.skipped == 0
    assert first.detail.get("horses_filled") == 3
    assert len(fetch_race_records(db, "HK_2024_K406")) == 1
    assert len(fetch_race_records(db, "HK_2024_K902")) == 1
    with get_connection(db) as conn:
        wv = conn.execute(
            "SELECT name_zh, brand_no FROM horses WHERE horse_id = ?",
            ("HK_2024_K902",),
        ).fetchone()
    assert wv is not None
    assert wv["name_zh"] == "風耀奪標"
    assert wv["brand_no"] == "K902"

    second = run_results_period_download(
        db,
        variables={
            "startDate": "2025-11-19",
            "endDate": "2025-11-19",
            "venueCodes": ["HV"],
        },
    )
    assert second.code == "success"
    assert second.progress.updated == 0
    assert second.progress.skipped == 3
    assert second.detail.get("horses_filled") == 0
    assert len(fetch_race_records(db, "HK_2024_K406")) == 1


def test_results_period_preset_whitelist(tmp_path: Path) -> None:
    requests_dir = tmp_path / "racing_requests"
    ensure_racing_preset_files(requests_dir)
    assert expected_query_for_preset(REQUEST_KEY_RESULTS_PERIOD) == RESULTS_PERIOD_QUERY
    payload, display = load_racing_request_preset(
        requests_dir, REQUEST_KEY_RESULTS_PERIOD
    )
    assert_payload_matches_whitelist(REQUEST_KEY_RESULTS_PERIOD, payload)
    # Load fills null dates with quoted ISO strings so Request box stays valid JSON.
    assert isinstance(payload.variables.get("startDate"), str)
    assert isinstance(payload.variables.get("endDate"), str)
    import json

    json.loads(display)
    bad = RacingRequestPayload(
        query="not-whitelisted",
        variables={"startDate": "2025-11-01", "endDate": "2025-11-02"},
        operation_name="localResultsPeriod",
    )
    try:
        assert_payload_matches_whitelist(REQUEST_KEY_RESULTS_PERIOD, bad)
        raise AssertionError("expected query_not_whitelisted")
    except RacingRequestPresetError as e:
        assert e.code == "query_not_whitelisted"
