"""Cross-module history search_op labels must share one canonical wording."""

from __future__ import annotations

from pathlib import Path

import pytest

from hppv.soccer_db import init_soccer_db
from hppv.translations import load_active_text_map, translate

# Shared visible condition options (same value + same meaning in soccer & racing).
SHARED_SEARCH_OP_SUFFIXES: tuple[str, ...] = (
    "label",
    "approx",
    "eq",
    "ne",
    "contains",
    "not_contains",
    "starts",
    "ends",
)

CANONICAL: dict[str, tuple[str, str, str]] = {
    "label": ("條件", "Condition", "条件"),
    "approx": ("近似", "Approximate", "近似"),
    "eq": ("等於", "Equals", "等于"),
    "ne": ("不等於", "Not equal", "不等于"),
    "contains": ("包含", "Contains", "包含"),
    "not_contains": ("不包含", "Does not contain", "不包含"),
    "starts": ("開頭是", "Starts with", "开头是"),
    "ends": ("結尾是", "Ends with", "结尾是"),
}


@pytest.mark.parametrize("suffix", SHARED_SEARCH_OP_SUFFIXES)
def test_shared_search_op_canonical_seed(tmp_path: Path, suffix: str) -> None:
    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    expected_zh, expected_en, expected_hans = CANONICAL[suffix]
    for lang, expected in (
        ("zh-Hant", expected_zh),
        ("en", expected_en),
        ("zh-Hans", expected_hans),
    ):
        m = load_active_text_map(db, lang)
        shared = translate(m, f"history.search_op.{suffix}")
        soccer = translate(m, f"soccer.history.search_op.{suffix}")
        racing = translate(m, f"racing.history.search_op.{suffix}")
        assert shared == expected
        assert soccer == expected
        assert racing == expected
        assert shared == soccer == racing


def test_racing_legacy_approx_renamed_on_existing_db(tmp_path: Path) -> None:
    """Existing DB with 「大約」 must be renamed to 「近似」 without clobbering customs."""
    from hppv.soccer_db import get_connection, init_soccer_db
    from hppv.translations import apply_seed_label_renames, seed_initial_translations

    db = tmp_path / "soccer.sqlite3"
    init_soccer_db(db)
    with get_connection(db) as conn:
        conn.execute(
            "UPDATE translations SET zh_hant=?, en=?, zh_hans=? "
            "WHERE text_key='racing.history.search_op.approx'",
            ("大約", "Approx", "大约"),
        )
        conn.commit()
        n = apply_seed_label_renames(conn)
        assert n >= 1
        seed_initial_translations(conn)
    m = load_active_text_map(db, "zh-Hant")
    assert translate(m, "racing.history.search_op.approx") == "近似"
    assert translate(m, "history.search_op.approx") == "近似"


def test_templates_use_shared_search_op_keys() -> None:
    racing = Path("hppv/web_templates/racing_info.html").read_text(encoding="utf-8")
    soccer = Path("hppv/web_templates/soccer_daily.html").read_text(encoding="utf-8")
    for suffix in SHARED_SEARCH_OP_SUFFIXES:
        shared = f"history.search_op.{suffix}"
        assert f"t('{shared}')" in racing
        assert f"t('{shared}')" in soccer
        # UI must not keep independent racing/soccer condition label sources.
        assert f"t('racing.history.search_op.{suffix}')" not in racing
        assert f"t('soccer.history.search_op.{suffix}')" not in soccer


def test_option_values_unchanged_in_templates() -> None:
    racing = Path("hppv/web_templates/racing_info.html").read_text(encoding="utf-8")
    soccer = Path("hppv/web_templates/soccer_daily.html").read_text(encoding="utf-8")
    for value in (
        "approx",
        "eq",
        "ne",
        "contains",
        "not_contains",
        "starts",
        "ends",
    ):
        assert f'value="{value}"' in racing
        assert f'value="{value}"' in soccer
