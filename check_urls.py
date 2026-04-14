"""一時腳本：測試從 selecthorsebychar 解析 (horse_id, name)。"""
import sys
sys.path.insert(0, "c:\\Hppv")
from hppv.scraper import (
    fetch_horse_page_html,
    _extract_horse_id_name_pairs_from_selecthorsebychar_html,
    _build_name_to_horse_id_from_selecthorsebychar,
)

url = "https://racing.hkjc.com/en-us/local/information/selecthorsebychar?ordertype=A"
html = fetch_horse_page_html(url, timeout_s=15)
pairs = _extract_horse_id_name_pairs_from_selecthorsebychar_html(html)
print(f"Page A: {len(pairs)} (horse_id, name) pairs")
for hid, name in pairs[:3]:
    print(f"  {hid} -> {name!r}")

# 只跑 2 頁試建表
from hppv.scraper import _SELECTHORSEBYCHAR_EN_BASE
name_to_id = {}
for ordertype in ["A", "B"]:
    html = fetch_horse_page_html(_SELECTHORSEBYCHAR_EN_BASE.format(ordertype=ordertype), timeout_s=15)
    for hid, name in _extract_horse_id_name_pairs_from_selecthorsebychar_html(html):
        n = name.strip()
        if n:
            name_to_id[n] = hid
print(f"Name->id size (A+B): {len(name_to_id)}")
# 用 Rating List 常見馬名測
print(" e.g. AERIS NOVA ->", name_to_id.get("AERIS NOVA"))
