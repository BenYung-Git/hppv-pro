from __future__ import annotations

from hppv.scraper import (
    header_index_going,
    header_index_venue,
    parse_past_performance_from_html,
)


def test_parse_empty_returns_no_records() -> None:
    html = "<html><body><h1>No tables</h1></body></html>"
    res = parse_past_performance_from_html("H1", html, "https://example.invalid/")
    assert res.horse_id == "H1"
    assert res.records == []


def test_header_index_venue_vs_going_exact_and_order() -> None:
    """場地 / 場地狀況 must resolve to different columns; order-independent."""
    headers_a = [
        "日期",
        "場地",
        "場次",
        "跑道",
        "途程",
        "場地狀況",
        "班次",
        "檔位",
        "評分",
        "負磅",
        "騎師",
        "練馬師",
        "名次",
        "沿途走勢",
        "獨贏賠率",
        "時間",
        "獎金",
        "x",
        "y",
    ]
    assert header_index_venue(headers_a) == 1
    assert header_index_going(headers_a) == 5

    headers_b = [
        "日期",
        "場地狀況",
        "場次",
        "跑道",
        "途程",
        "場地",
        "班次",
        "檔位",
        "評分",
        "負磅",
        "騎師",
        "練馬師",
        "名次",
        "沿途走勢",
        "獨贏賠率",
        "時間",
        "獎金",
        "x",
        "y",
    ]
    assert header_index_venue(headers_b) == 5
    assert header_index_going(headers_b) == 1


def test_header_index_venue_missing_does_not_use_going() -> None:
    """No 馬場 header → venue index is None (must not use 場地狀況 / 好)."""
    headers = [
        "日期",
        "場地狀況",
        "場次",
        "跑道",
        "途程",
        "班次",
        "檔位",
        "評分",
        "負磅",
        "騎師",
        "練馬師",
        "名次",
        "沿途走勢",
        "獨贏賠率",
        "時間",
        "獎金",
        "x",
        "y",
        "z",
    ]
    assert header_index_venue(headers) is None
    assert header_index_going(headers) == 1


def test_header_index_venue_compound_macau_track() -> None:
    """HKJC compound 「馬場/跑道/賽道」resolves as venue; spaced 場地狀況 as going."""
    headers = [
        "場次",
        "名次",
        "日期",
        "馬場/跑道/ 賽道",
        "途程",
        "場地 狀況",
        "賽事 班次",
        "檔位",
        "評分",
        "練馬師",
        "騎師",
        "頭馬 距離",
        "獨贏 賠率",
        "實際 負磅",
        "沿途 走位",
        "完成 時間",
        "排位 體重",
        "配備",
        "賽事 重播",
    ]
    assert header_index_venue(headers) == 3
    assert header_index_going(headers) == 5


def _perf_table_html(headers: list[str], cells: list[str]) -> str:
    th = "".join(f"<th>{h}</th>" for h in headers)
    td = "".join(f"<td>{c}</td>" for c in cells)
    # Wider table preference in parser: pad dummy columns if needed.
    while len(headers) < 10:
        headers = list(headers) + [f"col{len(headers)}"]
        cells = list(cells) + [""]
        th = "".join(f"<th>{h}</th>" for h in headers)
        td = "".join(f"<td>{c}</td>" for c in cells)
    return (
        "<html><body><table>"
        f"<tr>{th}</tr>"
        f"<tr>{td}</tr>"
        "</table></body></html>"
    )


def test_parse_venue_and_going_not_swapped() -> None:
    headers = [
        "日期",
        "場地",
        "場次",
        "跑道",
        "途程",
        "場地狀況",
        "班次",
        "檔位",
        "評分",
        "負磅",
        "騎師",
        "練馬師",
        "名次",
        "沿途走勢",
        "獨贏賠率",
        "時間",
        "獎金",
        "x",
        "y",
    ]
    cells = [
        "10/09/25",
        "跑馬地",
        "012",
        '跑馬地草地"A"',
        "1650",
        "好",
        "5",
        "9",
        "40",
        "131",
        "布文",
        "方嘉柏",
        "01",
        "1 1 1 1",
        "5.6",
        "1.40.30",
        "",
        "",
        "",
    ]
    res = parse_past_performance_from_html(
        "HK_2024_K306", _perf_table_html(headers, cells), "https://example.invalid/"
    )
    assert len(res.records) == 1
    rec = res.records[0]
    assert rec.venue == "跑馬地"
    assert rec.going == "好"
    assert rec.course == '跑馬地草地"A"'


def test_parse_going_only_header_derives_venue_from_course() -> None:
    """No 場地 header: going stays 好; venue derived from course (not from going)."""
    headers = [
        "日期",
        "場地狀況",
        "場次",
        "跑道",
        "途程",
        "班次",
        "檔位",
        "評分",
        "負磅",
        "騎師",
        "練馬師",
        "名次",
        "沿途走勢",
        "獨贏賠率",
        "時間",
        "獎金",
        "x",
        "y",
        "z",
    ]
    cells = [
        "10/09/25",
        "好",
        "012",
        '跑馬地草地"A"',
        "1650",
        "5",
        "9",
        "40",
        "131",
        "布文",
        "方嘉柏",
        "01",
        "1 1 1 1",
        "5.6",
        "1.40.30",
        "",
        "",
        "",
        "",
    ]
    res = parse_past_performance_from_html(
        "HK_2024_K306", _perf_table_html(headers, cells), "https://example.invalid/"
    )
    assert len(res.records) == 1
    rec = res.records[0]
    assert rec.venue == "跑馬地"
    assert rec.going == "好"
    assert rec.course == '跑馬地草地"A"'


def test_parse_compound_venue_course_header_keeps_local_rows() -> None:
    """Compound 馬場/跑道 cell still yields local venue + rating (foreign filter)."""
    headers = [
        "場次",
        "名次",
        "日期",
        "馬場/跑道/ 賽道",
        "途程",
        "場地 狀況",
        "賽事 班次",
        "檔位",
        "評分",
        "練馬師",
        "騎師",
        "頭馬 距離",
        "獨贏 賠率",
        "實際 負磅",
        "沿途 走位",
        "完成 時間",
        "排位 體重",
        "配備",
        "賽事 重播",
    ]
    cells = [
        "219",
        "06",
        "25/11/26",
        '跑馬地草地"C+3"',
        "1200",
        "好",
        "5",
        "10",
        "37",
        "游達榮",
        "艾兆禮",
        "3-1/4",
        "29",
        "128",
        "12 12 6",
        "1.10.32",
        "1174",
        "TT",
        "",
    ]
    res = parse_past_performance_from_html(
        "HK_2024_K189", _perf_table_html(headers, cells), "https://example.invalid/"
    )
    assert len(res.records) == 1
    rec = res.records[0]
    assert rec.venue == "跑馬地"
    assert rec.rating == "37"
    assert rec.race_no == "219"
