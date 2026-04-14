"""檢查「一先生」在 selecthorsebychar 的 (horse_id, name) 對應。"""
import sys
sys.path.insert(0, "c:\\Hppv")

from hppv.scraper import (
    fetch_horse_page_html,
    _extract_horse_id_name_pairs_from_selecthorsebychar_html,
)

_SELECTHORSEBYCHAR_ZH_BASE = "https://racing.hkjc.com/zh-hk/local/information/selecthorsebychar?ordertype={ordertype}"
_SELECTHORSEBYCHAR_EN_BASE = "https://racing.hkjc.com/en-us/local/information/selecthorsebychar?ordertype={ordertype}"

# 先查中文頁 (0-4)，「一」通常在筆畫/部首
for ordertype in [0, 1, 2, 3, 4]:
    url = _SELECTHORSEBYCHAR_ZH_BASE.format(ordertype=ordertype)
    try:
        html = fetch_horse_page_html(url, timeout_s=20.0)
        pairs = _extract_horse_id_name_pairs_from_selecthorsebychar_html(html)
        for hid, name in pairs:
            if "一" in name or "先生" in name or "G227" in name or name.strip() == "一先生":
                print(f"ZH ordertype={ordertype}: {hid!r} -> {name!r}")
        if ordertype == 0:
            # 列出 ordertype=0 前 20 個名字，看格式
            print(f"  (ordertype=0 total {len(pairs)} horses, first 15 names: {[p[1] for p in pairs[:15]]})")
    except Exception as e:
        print(f"ZH ordertype={ordertype}: error {e}")

# 英文頁找 "MR" 或可能對應的英文名
print("\n--- EN pages (looking for possible English name for 一先生) ---")
for letter in ["M", "O", "O"]:
    url = _SELECTHORSEBYCHAR_EN_BASE.format(ordertype=letter)
    try:
        html = fetch_horse_page_html(url, timeout_s=20.0)
        pairs = _extract_horse_id_name_pairs_from_selecthorsebychar_html(html)
        for hid, name in pairs:
            if "ONE" in name.upper() or "MR" in name.upper() or "YI" in name.upper():
                print(f"EN {letter}: {hid!r} -> {name!r}")
    except Exception as e:
        print(f"EN {letter}: error {e}")
