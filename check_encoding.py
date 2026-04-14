"""檢查修正後「一先生」能否從 selecthorsebychar 取得 horse_id。"""
import sys
sys.path.insert(0, "c:\\Hppv")
from hppv.scraper import (
    fetch_horse_page_html,
    _extract_horse_id_name_pairs_from_selecthorsebychar_html,
)

url = "https://racing.hkjc.com/zh-hk/local/information/selecthorsebychar?ordertype=3"
html = fetch_horse_page_html(url, timeout_s=20)
pairs = _extract_horse_id_name_pairs_from_selecthorsebychar_html(html)

# 寫出該頁全部馬名與碼位，找出對應 G227 / 一先生 的 horse_id
out_path = "c:\\Hppv\\names_ordertype3.txt"
with open(out_path, "w", encoding="utf-8") as f:
    for hid, name in pairs:
        hexs = " ".join(f"U+{ord(c):04X}" for c in name)
        f.write(f"{hid}\t{name}\t{hexs}\n")

yixiansheng = "\u4e00\u5148\u751f"  # 一先生
for hid, name in pairs:
    if name == yixiansheng:
        print("Exact 一先生:", hid)
        break
else:
    # 找 先(U+5148)+生(U+751F) 且長度 3
    for hid, name in pairs:
        if len(name) == 3 and ord(name[1]) == 0x5148 and ord(name[2]) == 0x751F:
            print("Match ?先生:", hid, "first char U+%04X" % ord(name[0]))
    print("Wrote", out_path)
