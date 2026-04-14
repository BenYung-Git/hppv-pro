from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Application configuration (paths, endpoints)."""

    data_dir: Path
    db_path: Path
    hkjc_horse_url_template: str


def default_config() -> AppConfig:
    """Create the default configuration for local runs."""

    # PyInstaller 打包後：資料目錄改放在 exe 同層，方便內測同事直接雙擊執行與寫入 DB。
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
    else:
        root = Path(__file__).resolve().parents[1]
    data_dir = root / "data"
    db_path = data_dir / "hppv.sqlite3"
    return AppConfig(
        data_dir=data_dir,
        db_path=db_path,
        # HKJC pages can change; keep this centralized.
        hkjc_horse_url_template="https://racing.hkjc.com/racing/information/Chinese/Horse/Horse.aspx?HorseId={horse_id}",
    )


# HKJC horse index pages (for building the `horses` table).
HKJC_HORSE_SELECT_URL_ZH: str = "https://racing.hkjc.com/racing/information/chinese/Horse/SelectHorse.aspx"
HKJC_HORSE_SELECT_URL_EN: str = "https://racing.hkjc.com/racing/information/english/Horse/SelectHorse.aspx"

# HKJC horse detail pages (for extracting horse names).
HKJC_HORSE_DETAIL_URL_ZH: str = (
    "https://racing.hkjc.com/racing/information/Chinese/Horse/Horse.aspx?HorseId={horse_id}"
)
HKJC_HORSE_DETAIL_URL_EN: str = (
    "https://racing.hkjc.com/racing/information/english/Horse/Horse.aspx?HorseId={horse_id}"
)

# Horse rating list pages (for building `horses` table).
HKJC_MCS_HORSES_CLAS_URL_EN: str = "https://racing.hkjc.com/racing/info/mcs/English/Horses/clas/"
HKJC_LATESTONHORSE_HORSES_CLAS_URL_EN: str = (
    "https://racing.hkjc.com/en-us/local/information/latestonhorse?View=Horses%2Fclas%2F"
)


# selecthorsebychar（依字母/筆畫，用於取得 HorseId；HTML 含 horseid= 連結）
HKJC_SELECTHORSEBYCHAR_EN: str = "https://racing.hkjc.com/en-us/local/information/selecthorsebychar?ordertype=A"
HKJC_SELECTHORSEBYCHAR_ZH: str = "https://racing.hkjc.com/zh-hk/local/information/selecthorsebychar?ordertype=0"


def horses_data_source_urls() -> list[str]:
    """URL 列表：馬匹資料（更新馬匹資料）從這些網址抓取。"""
    return [
        HKJC_MCS_HORSES_CLAS_URL_EN,
        HKJC_LATESTONHORSE_HORSES_CLAS_URL_EN,
        HKJC_SELECTHORSEBYCHAR_EN + " (A–Z)",
        HKJC_SELECTHORSEBYCHAR_ZH + " (0–4)",
    ]



