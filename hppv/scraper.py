from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable

import requests
from bs4 import BeautifulSoup

from .models import RaceRecord
from .db import get_connection, is_acceptable_horse_id, upsert_horses
from .config import (
    HKJC_HORSE_SELECT_URL_EN,
    HKJC_HORSE_SELECT_URL_ZH,
    HKJC_LATESTONHORSE_HORSES_CLAS_URL_EN,
    HKJC_MCS_HORSES_CLAS_URL_EN,
)


@dataclass(frozen=True, slots=True)
class ScrapeResult:
    """Scrape outcome for a horse."""

    horse_id: str
    records: list[RaceRecord]
    source_url: str


def fetch_horse_page_html(url: str, timeout_s: float = 20.0) -> str:
    """Download the horse page HTML."""

    headers = {
        "User-Agent": "HPPV/0.1 (+requests; past-performance viewer)",
        "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
    }
    resp = requests.get(url, headers=headers, timeout=timeout_s)
    resp.raise_for_status()
    # HKJC 中文頁 (zh-hk) 為 UTF-8；不明確指定時 apparent_encoding 可能猜錯，導致「一先生」等無法對應
    if "zh-hk" in url.lower():
        resp.encoding = "utf-8"
    else:
        resp.encoding = resp.apparent_encoding
    return resp.text


def _clean_cell_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_past_performance_from_html(horse_id: str, html: str, source_url: str) -> ScrapeResult:
    """Parse past performance table into normalized rows.

    Note: HKJC markup changes over time; this parser targets a resilient baseline:
    - find tables with many columns
    - parse header + body rows
    - keep raw text fields (no analytics)
    """

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")

    candidate_tables: list[tuple[int, int, object]] = []
    date_pat = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
    for t in tables:
        rows = t.find_all("tr")
        if len(rows) < 2:
            continue
        max_cols = 0
        date_hits = 0
        for r in rows[:5]:
            cols = r.find_all(["th", "td"])
            max_cols = max(max_cols, len(cols))
        # Count date-like cells in more rows to identify the true performance table.
        for r in rows[:80]:
            cols = r.find_all(["th", "td"])
            if len(cols) < 5:
                continue
            texts = [_clean_cell_text(c.get_text(" ", strip=True)) for c in cols]
            if any(date_pat.search(tx) for tx in texts):
                date_hits += 1
        if max_cols >= 10:
            candidate_tables.append((date_hits, max_cols, t))

    if not candidate_tables:
        return ScrapeResult(horse_id=horse_id, records=[], source_url=source_url)

    # Prefer the table with the most date-like rows, then wider columns.
    candidate_tables.sort(key=lambda x: (x[0], x[1]), reverse=True)
    table = candidate_tables[0][2]

    header_cells = table.find("tr").find_all(["th", "td"])
    headers = [_clean_cell_text(c.get_text(" ", strip=True)) for c in header_cells]

    def idx_of(*keys: str) -> int | None:
        for k in keys:
            for i, h in enumerate(headers):
                if k in h:
                    return i
        return None

    idx_date = idx_of("日期", "Date")
    idx_venue = idx_of("場地", "Venue")
    idx_race_no = idx_of("場次", "Race")
    idx_course = idx_of("跑道", "Course", "途程", "Track")
    idx_dist = idx_of("途程", "Distance", "米")
    idx_going = idx_of("場地狀況", "Going")
    idx_class = idx_of("班", "Class")
    idx_draw = idx_of("檔", "Draw")
    idx_rating = idx_of("評分", "Rating")
    idx_jockey = idx_of("騎師", "Jockey")
    idx_trainer = idx_of("練馬師", "Trainer")
    idx_pos = idx_of("名次", "Pos", "Finish")
    idx_running_pos = idx_of("沿途", "Running", "Position")
    idx_win_odds = idx_of("獨贏", "Win Odds", "Odds")
    idx_time = idx_of("時間", "Time")
    idx_prize = idx_of("獎金", "Prize")

    body_rows = table.find_all("tr")[1:]
    records: list[RaceRecord] = []
    seen_keys: set[tuple[str, ...]] = set()
    for r in body_rows:
        cols = r.find_all("td")
        if not cols:
            continue
        texts = [_clean_cell_text(c.get_text(" ", strip=True)) for c in cols]

        if len(texts) < 8:
            continue

        def get(i: int | None, fallback: str = "") -> str:
            if i is None:
                return fallback
            if i < 0 or i >= len(texts):
                return fallback
            return texts[i]

        if idx_date is None:
            # Fallback: detect date column by pattern in current row.
            for i, tx in enumerate(texts):
                if date_pat.search(tx):
                    idx_date = i
                    break
            if idx_date is None and texts:
                idx_date = 0
        # Fallback for HKJC 19-col table layout.
        if len(texts) >= 19:
            if idx_going is None:
                idx_going = 5
            if idx_running_pos is None:
                idx_running_pos = 14
            if idx_win_odds is None:
                idx_win_odds = 12

        rec = RaceRecord(
            horse_id=horse_id,
            race_date=get(idx_date),
            venue=get(idx_venue),
            race_no=get(idx_race_no),
            course=get(idx_course),
            distance=get(idx_dist),
            going=get(idx_going),
            class_band=get(idx_class),
            draw=get(idx_draw),
            rating=get(idx_rating),
            jockey=get(idx_jockey),
            trainer=get(idx_trainer),
            finish_pos=get(idx_pos),
            running_pos=get(idx_running_pos),
            win_odds=get(idx_win_odds),
            time=get(idx_time),
            prize=get(idx_prize),
        )
        if not rec.race_date:
            continue
        dedup_key = (
            rec.race_date.strip(),
            rec.venue.strip(),
            rec.race_no.strip(),
            rec.course.strip(),
            rec.distance.strip(),
            rec.going.strip(),
            rec.class_band.strip(),
            rec.draw.strip(),
            rec.rating.strip(),
            rec.jockey.strip(),
            rec.trainer.strip(),
            rec.finish_pos.strip(),
            rec.running_pos.strip(),
            rec.win_odds.strip(),
            rec.time.strip(),
            rec.prize.strip(),
        )
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        records.append(rec)

    return ScrapeResult(horse_id=horse_id, records=records, source_url=source_url)


# 烙號（如 D241）→ HKJC HorseId（HK_…）：首次查詢會抓多頁 selecthorsebychar，之後快取於記憶體。
_brand_to_hkjc_cache: dict[str, str] | None = None


def clear_brand_to_hkjc_cache() -> None:
    """測試或強制重新對照烙號時可呼叫。"""
    global _brand_to_hkjc_cache
    _brand_to_hkjc_cache = None


def get_brand_to_hkjc_map() -> dict[str, str]:
    """HKJC 全站烙號→HorseId 對照（鍵為大寫烙號）。"""
    global _brand_to_hkjc_cache
    if _brand_to_hkjc_cache is None:
        _, raw = _build_mappings_from_selecthorsebychar()
        _brand_to_hkjc_cache = {str(k).strip().upper(): v for k, v in raw.items()}
    return _brand_to_hkjc_cache


def resolve_brand_no_to_hkjc_id(brand_no: str) -> str | None:
    """將烙號（如 D241）轉成 HKJC 的 HorseId；無對照則 None。"""
    b = str(brand_no or "").strip().upper()
    if not re.fullmatch(r"[A-Z]\d{3}", b):
        return None
    return get_brand_to_hkjc_map().get(b)


def resolve_hkjc_fetch_ids(db_path: Path, horse_id: str) -> tuple[str, str]:
    """
    回傳 (用於 Horse.aspx?HorseId= 的 id, 寫入 race_records / UI 用的 horse_id)。

    HKJC 網址只接受 HK_ 開頭的 HorseId；若 horses 表仍以烙號當主鍵（舊資料），
    必須先對應到真實 HK_，否則會抓到空頁或錯頁（例如 D241）。
    """
    hid = str(horse_id or "").strip()
    if not hid:
        return ("", "")

    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT horse_id, brand_no FROM horses
            WHERE horse_id = ? OR UPPER(TRIM(COALESCE(brand_no, ''))) = UPPER(?);
            """,
            (hid, hid),
        ).fetchall()

    hk_rows = [r for r in rows if str(r["horse_id"] or "").strip().upper().startswith("HK_")]
    if hk_rows:
        x = str(hk_rows[0]["horse_id"]).strip()
        return (x, x)

    if rows:
        storage_id = str(rows[0]["horse_id"] or "").strip()
        brand_key = str(rows[0]["brand_no"] or "").strip() or storage_id
        with get_connection(db_path) as conn:
            same_brand = conn.execute(
                """
                SELECT horse_id FROM horses
                WHERE UPPER(TRIM(COALESCE(brand_no, ''))) = UPPER(?);
                """,
                (brand_key,),
            ).fetchall()
        for r in same_brand:
            sid = str(r["horse_id"] or "").strip()
            if sid.upper().startswith("HK_"):
                return (sid, storage_id)
        mapped = resolve_brand_no_to_hkjc_id(brand_key)
        if mapped:
            return (mapped, storage_id)
        return (storage_id, storage_id)

    if is_acceptable_horse_id(hid) and hid.upper().startswith("HK_"):
        return (hid, hid)
    if re.fullmatch(r"[A-Za-z]\d{3}", hid):
        mapped = resolve_brand_no_to_hkjc_id(hid.upper())
        if mapped:
            return (mapped, mapped)
    return (hid, hid)


def scrape_horse_past_performance(
    horse_id: str,
    url_template: str,
    *,
    db_path: Path | None = None,
) -> ScrapeResult:
    """Fetch and parse a horse's past performance from HKJC.

    若提供 `db_path`，會依 horses 表／HKJC 烙號對照將 URL 改為正確的 HK_ HorseId，
    同時仍把往績寫在原本的主鍵 horse_id 下（與列表一致）。
    """

    if db_path is not None:
        url_hid, store_hid = resolve_hkjc_fetch_ids(db_path, horse_id)
    else:
        url_hid = store_hid = horse_id

    url = url_template.format(horse_id=url_hid)
    # HKJC horse page default may show only recent seasons; force Option=1 for all history.
    if "option=" not in url.lower():
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}Option=1"
    html = fetch_horse_page_html(url)
    return parse_past_performance_from_html(horse_id=store_hid, html=html, source_url=url)


@dataclass(frozen=True, slots=True)
class HorseBasicInfo:
    """Horse basic information for the `horses` table."""

    horse_id: str
    brand_no: str | None = None
    name_zh: str | None = None
    name_en: str | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class HorsesUpdateStats:
    """Stats for updating the `horses` table."""

    total_found: int
    written: int


def _horse_id_from_href(href: str) -> str | None:
    m = re.search(r"[?&]horseid=([^&\s]+)", href, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip()


_BRAND_NO_STRICT = re.compile(r"^[A-Z]\d{3}$")


def _rating_list_row_looks_like_header(name_en: str, name_zh: str, brand_no: str) -> bool:
    """是否像表頭列（例如 Name / Name / Brand No.）。"""
    ne, nz, b = name_en.strip().lower(), name_zh.strip().lower(), brand_no.strip().lower()
    if ne in {"name", "horse", "brand", "rating"} or nz in {"name", "horse", "brand", "rating"}:
        return True
    if b in ("brand no", "brand no.", "no.", "no"):
        return True
    if ne == "name" and nz == "name":
        return True
    return False


def _parse_select_horses_page(html: str) -> Dict[str, str]:
    """Parse HKJC SelectHorse page (heuristic).

    TODO: If the selector breaks, inspect the SelectHorse table columns and
    tighten the anchor filtering based on the actual DOM.
    """

    soup = BeautifulSoup(html, "html.parser")
    mapping: Dict[str, str] = {}

    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if "horseid=" not in href.lower():
            continue

        horse_id = _horse_id_from_href(href)
        if not horse_id:
            continue

        name = _clean_cell_text(a.get_text(" ", strip=True))
        if not name:
            name = _clean_cell_text(str(a.get("title", "") or ""))
        if not name:
            continue

        mapping[horse_id] = name

    return mapping


def _build_name_to_horse_id_from_select_horse() -> Dict[str, str]:
    """從 SelectHorse 頁取得 horse_id -> name，回傳 name -> horse_id（供以馬名對應）。"""
    name_to_id: Dict[str, str] = {}
    for url in (HKJC_HORSE_SELECT_URL_EN, HKJC_HORSE_SELECT_URL_ZH):
        try:
            html = fetch_horse_page_html(url, timeout_s=30.0)
        except Exception:
            continue
        id_to_name = _parse_select_horses_page(html)
        for hid, name in id_to_name.items():
            n = re.sub(r"\s+", " ", name).strip()
            if n:
                name_to_id[n] = hid
    return name_to_id


def fetch_all_horses_basic_info() -> list[HorseBasicInfo]:
    """Fetch horse basic info from HKJC rating list table.

    - Parse "Horse Rating List (Order by Name)" table: name_en, name_zh, brand_no；
      horse_id 從列內連結 HorseId= 取得，若無則先用 brand_no。
    - 若仍有 horse_id == brand_no，再以 SelectHorse 頁的馬名對照表補上真實 HorseId。
    """

    urls: list[str] = [HKJC_MCS_HORSES_CLAS_URL_EN, HKJC_LATESTONHORSE_HORSES_CLAS_URL_EN]
    basics: list[HorseBasicInfo] = []
    for url in urls:
        html = fetch_horse_page_html(url, timeout_s=30.0)
        basics = _parse_horse_rating_list_from_html(html)
        if basics:
            break
    if not basics:
        return []

    # 仍以 brand_no 當 horse_id 的列，先用 brand_no 對照，再用馬名對照補上 HK_xxxx_xxxx
    name_to_id, brand_to_id = _build_mappings_from_selecthorsebychar()
    global _brand_to_hkjc_cache
    if brand_to_id:
        _brand_to_hkjc_cache = {str(k).strip().upper(): v for k, v in brand_to_id.items()}
    if name_to_id or brand_to_id:
        def norm(s: str | None) -> str:
            return re.sub(r"\s+", " ", (s or "").strip())

        out: list[HorseBasicInfo] = []
        for h in basics:
            horse_id = h.horse_id
            if horse_id == h.brand_no:
                n_en = norm(h.name_en)
                n_zh = norm(h.name_zh)
                bno = norm(h.brand_no)
                b_key = bno.strip().upper() if bno else ""
                horse_id = (
                    brand_to_id.get(b_key)
                    or name_to_id.get(n_en)
                    or name_to_id.get(n_zh)
                    or horse_id
                )
            out.append(
                HorseBasicInfo(
                    horse_id=horse_id,
                    brand_no=h.brand_no,
                    name_en=h.name_en,
                    name_zh=h.name_zh,
                    status=h.status,
                )
            )
        return out
    return basics


def update_horses_table_from_web(db_path: Path) -> HorsesUpdateStats:
    """Fetch horse list from HKJC and update the local `horses` table."""
    basics = fetch_all_horses_basic_info()
    valid = [h for h in basics if is_acceptable_horse_id(h.horse_id)]
    if not valid:
        return HorsesUpdateStats(total_found=0, written=0)

    written = upsert_horses(
        db_path,
        [
            {
                "horse_id": h.horse_id,
                "brand_no": h.brand_no,
                "name_zh": h.name_zh,
                "name_en": h.name_en,
                "status": h.status,
            }
            for h in valid
        ],
    )
    return HorsesUpdateStats(total_found=len(valid), written=written)


def _parse_horse_rating_list_from_html(html: str) -> list[HorseBasicInfo]:
    """Parse horse rating list table into HorseBasicInfo rows.

    horse_id: from <a href="...HorseId=..."> in the row if present, else brand_no.
    brand_no: from Brand No. column.
    TODO: If HKJC changes the table DOM, adjust the table selection heuristic.
    """

    soup = BeautifulSoup(html, "html.parser")

    def clean(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    # Find a table whose **首列**像表頭：同時含 Brand / Rating / Name（避免掃整張表把資料列誤當表頭）。
    for table in soup.find_all("table"):
        header_tr = table.find("tr")
        if header_tr is None:
            continue
        header_cells = header_tr.find_all(["th", "td"])
        if len(header_cells) < 3:
            continue
        header_blob = " ".join(clean(c.get_text(" ", strip=True)) for c in header_cells).lower()
        if "brand" not in header_blob or "rating" not in header_blob:
            continue
        if "name" not in header_blob and "horse" not in header_blob:
            continue

        rows: list[HorseBasicInfo] = []
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue

            name_en = clean(tds[0].get_text(" ", strip=True))
            name_zh = clean(tds[1].get_text(" ", strip=True))
            brand_no = clean(tds[2].get_text(" ", strip=True))

            if not name_en or not brand_no:
                continue

            # 跳過表頭或異常大段文字（整頁被誤併成一列）
            if _rating_list_row_looks_like_header(name_en, name_zh, brand_no):
                continue
            if len(name_en) > 80 or len(name_zh) > 80:
                continue
            if not _BRAND_NO_STRICT.match(brand_no):
                continue

            # 從同一列內的連結取得 HorseId（HK_xxxx_xxxx），沒有則用 brand_no
            horse_id: str = brand_no
            for a in tr.find_all("a", href=True):
                hid = _horse_id_from_href(str(a["href"]))
                if hid and is_acceptable_horse_id(hid):
                    horse_id = hid
                    break

            if not is_acceptable_horse_id(horse_id):
                continue

            rows.append(
                HorseBasicInfo(
                    horse_id=horse_id,
                    brand_no=brand_no,
                    name_en=name_en,
                    name_zh=name_zh or None,
                    status=None,
                )
            )

        if rows:
            return rows

    return []


def _fetch_horse_ids_from_selecthorsebychar_pages() -> set[str]:
    """Fetch all horse ids from HKJC selecthorsebychar pages."""

    zh_ordertype_values: list[int] = [0, 1, 2, 3, 4]
    en_ordertype_values: list[str] = [chr(c) for c in range(ord("A"), ord("Z") + 1)]

    zh_select_base = "https://racing.hkjc.com/zh-hk/local/information/selecthorsebychar?ordertype={ordertype}"
    en_select_base = "https://racing.hkjc.com/en-us/local/information/selecthorsebychar?ordertype={ordertype}"

    horse_ids: set[str] = set()

    for ordertype in zh_ordertype_values:
        url = zh_select_base.format(ordertype=ordertype)
        html = fetch_horse_page_html(url, timeout_s=30.0)
        horse_ids.update(_extract_horse_ids_from_selecthorsebychar_html(html))

    for ordertype in en_ordertype_values:
        url = en_select_base.format(ordertype=ordertype)
        html = fetch_horse_page_html(url, timeout_s=30.0)
        horse_ids.update(_extract_horse_ids_from_selecthorsebychar_html(html))

    return horse_ids


def _extract_horse_ids_from_selecthorsebychar_html(html: str) -> list[str]:
    """Extract horse ids from `selecthorsebychar` HTML."""

    soup = BeautifulSoup(html, "html.parser")
    ids: list[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if "information/horse" not in href or "horseid=" not in href:
            continue
        m = re.search(r"[?&]horseid=([^&]+)", href, flags=re.IGNORECASE)
        if not m:
            continue
        hid = m.group(1).strip()
        if hid:
            ids.append(hid)
    return ids


# 中文頁 ordertype=0 的連結文字可能是 Brand No.（如 L372），非馬名，不加入對照
_BRAND_NO_LIKE = re.compile(r"^[A-Z]\d{3}$")


def _extract_horse_id_name_pairs_from_selecthorsebychar_html(html: str) -> list[tuple[str, str]]:
    """從 selecthorsebychar 頁解析 (horse_id, 馬名) 列表。"""
    soup = BeautifulSoup(html, "html.parser")
    pairs: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if "horseid=" not in href.lower():
            continue
        m = re.search(r"[?&]horseid=([^&\s]+)", href, flags=re.IGNORECASE)
        if not m:
            continue
        hid = m.group(1).strip()
        name = _clean_cell_text(a.get_text(" ", strip=True))
        if not hid or not name:
            continue
        pairs.append((hid, name))
    return pairs


# selecthorsebychar 網址（與 _fetch_horse_ids_from_selecthorsebychar_pages 一致）
_SELECTHORSEBYCHAR_EN_BASE = "https://racing.hkjc.com/en-us/local/information/selecthorsebychar?ordertype={ordertype}"
_SELECTHORSEBYCHAR_ZH_BASE = "https://racing.hkjc.com/zh-hk/local/information/selecthorsebychar?ordertype={ordertype}"


def _build_mappings_from_selecthorsebychar() -> tuple[Dict[str, str], Dict[str, str]]:
    """從 selecthorsebychar 建立兩種對照：馬名->horse_id、brand_no->horse_id。"""
    name_to_id: Dict[str, str] = {}
    brand_to_id: Dict[str, str] = {}
    en_letters: list[str] = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    zh_types: list[int] = [0, 1, 2, 3, 4]
    for ordertype in en_letters:
        url = _SELECTHORSEBYCHAR_EN_BASE.format(ordertype=ordertype)
        try:
            html = fetch_horse_page_html(url, timeout_s=25.0)
        except Exception:
            continue
        for hid, name in _extract_horse_id_name_pairs_from_selecthorsebychar_html(html):
            n = re.sub(r"\s+", " ", name).strip()
            if not n:
                continue
            if _BRAND_NO_LIKE.match(n):
                brand_to_id[n.strip().upper()] = hid
            else:
                name_to_id[n] = hid
    for ordertype in zh_types:
        url = _SELECTHORSEBYCHAR_ZH_BASE.format(ordertype=ordertype)
        try:
            html = fetch_horse_page_html(url, timeout_s=25.0)
        except Exception:
            continue
        for hid, name in _extract_horse_id_name_pairs_from_selecthorsebychar_html(html):
            n = re.sub(r"\s+", " ", name).strip()
            if not n:
                continue
            if _BRAND_NO_LIKE.match(n):
                brand_to_id[n.strip().upper()] = hid
            else:
                name_to_id[n] = hid
    return name_to_id, brand_to_id


def _fetch_horse_name_from_detail_page(url: str) -> str | None:
    """Fetch a horse detail page and extract display name from its title."""

    html = fetch_horse_page_html(url, timeout_s=20.0)
    soup = BeautifulSoup(html, "html.parser")
    if not soup.title or not soup.title.get_text(strip=True):
        return None
    title = soup.title.get_text(" ", strip=True)
    # Example: "UNNAMED - Horses - Horse Racing - ..."
    # Example: "<HorseName> - 馬匹資料 - ..."
    parts = [p.strip() for p in title.split(" - ") if p.strip()]
    return parts[0] if parts else None

