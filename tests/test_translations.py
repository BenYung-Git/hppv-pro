from __future__ import annotations

from pathlib import Path

import pytest

from hppv.schema import translations_expected_columns
from hppv.soccer_db import init_soccer_db
from hppv.translations import (
    TranslationError,
    apply_seed_label_renames,
    create_translation,
    list_translations,
    load_active_text_map,
    seed_initial_translations,
    set_translation_disabled,
    translate,
    update_translation,
)
from hppv.soccer_db import get_connection


def test_init_creates_translations_and_seeds(tmp_path: Path) -> None:
    db_path = tmp_path / "soccer.sqlite3"
    result = init_soccer_db(db_path)
    assert result["translations_inserted"] > 0

    with get_connection(db_path) as conn:
        cols = {
            str(r["name"])
            for r in conn.execute("PRAGMA table_info(translations);").fetchall()
        }
        assert cols == translations_expected_columns()
        count = conn.execute("SELECT COUNT(*) AS c FROM translations;").fetchone()
        assert int(count["c"]) == result["translations_inserted"]

    # Idempotent: second init inserts 0 and keeps data.
    result2 = init_soccer_db(db_path)
    assert result2["translations_inserted"] == 0
    with get_connection(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM translations;").fetchone()
        assert int(count["c"]) == result["translations_inserted"]


def test_seed_does_not_overwrite_user_edits(tmp_path: Path) -> None:
    db_path = tmp_path / "soccer.sqlite3"
    init_soccer_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE translations SET zh_hant = ? WHERE text_key = ?",
            ("使用者修改", "soccer.daily.title"),
        )
        seed_initial_translations(conn)
        row = conn.execute(
            "SELECT zh_hant FROM translations WHERE text_key = ?",
            ("soccer.daily.title",),
        ).fetchone()
        assert row["zh_hant"] == "使用者修改"


def test_crud_search_filter_and_toggle(tmp_path: Path) -> None:
    db_path = tmp_path / "soccer.sqlite3"
    init_soccer_db(db_path)

    item = create_translation(
        db_path,
        text_key="test.hello",
        zh_hant="你好",
        en="Hello",
        zh_hans="你好",
    )
    assert item["isDisable"] is False

    with pytest.raises(TranslationError) as exc:
        create_translation(
            db_path,
            text_key="test.hello",
            zh_hant="重複",
            en="Dup",
            zh_hans="重复",
        )
    assert str(exc.value) == "key_duplicate"

    with pytest.raises(TranslationError):
        create_translation(
            db_path,
            text_key="  ",
            zh_hant="a",
            en="b",
            zh_hans="c",
        )

    updated = update_translation(
        db_path,
        item["TxId"],
        zh_hant="哈囉",
        en="Hello!",
        zh_hans="哈喽",
    )
    assert updated["zh_hant"] == "哈囉"

    found = list_translations(db_path, q="Hello!", status="all")
    assert any(r["TxId"] == item["TxId"] for r in found)

    disabled = set_translation_disabled(db_path, item["TxId"], True)
    assert disabled["isDisable"] is True
    active_only = list_translations(db_path, q="test.hello", status="active")
    assert active_only == []
    disabled_only = list_translations(db_path, q="test.hello", status="disabled")
    assert len(disabled_only) == 1

    text_map = load_active_text_map(db_path, "en")
    assert "test.hello" not in text_map
    assert translate(text_map, "test.hello") == "[test.hello]"


def test_translate_active_key(tmp_path: Path) -> None:
    db_path = tmp_path / "soccer.sqlite3"
    init_soccer_db(db_path)
    text_map = load_active_text_map(db_path, "en")
    assert translate(text_map, "soccer.daily.title") == "Soccer Database"
    assert translate(text_map, "soccer.daily.tab.history") == "Match History"
    text_map_hant = load_active_text_map(db_path, "zh-Hant")
    assert translate(text_map_hant, "nav.soccer_daily") == "足球資料庫"
    assert translate(text_map_hant, "soccer.daily.tab.history") == "球賽往績"
    assert translate(load_active_text_map(db_path, "zh-Hans"), "soccer.daily.tab.history") == "球赛往绩"
    text_map_zh = load_active_text_map(db_path, "zh-Hans")
    assert translate(text_map_zh, "soccer.daily.under_construction") == "功能建设中"


def test_seed_label_renames_only_when_old_defaults(tmp_path: Path) -> None:
    db_path = tmp_path / "soccer.sqlite3"
    init_soccer_db(db_path)
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE translations SET zh_hant=?, en=?, zh_hans=? WHERE text_key=?",
            ("足球每日賽事", "Daily Soccer Matches", "足球每日赛事", "nav.soccer_daily"),
        )
        conn.execute(
            "UPDATE translations SET zh_hant=? WHERE text_key=?",
            ("使用者自訂標題", "soccer.daily.title"),
        )
        n = apply_seed_label_renames(conn)
        nav = conn.execute(
            "SELECT zh_hant, en, zh_hans FROM translations WHERE text_key=?",
            ("nav.soccer_daily",),
        ).fetchone()
        title = conn.execute(
            "SELECT zh_hant FROM translations WHERE text_key=?",
            ("soccer.daily.title",),
        ).fetchone()
    assert n == 1
    assert (nav["zh_hant"], nav["en"], nav["zh_hans"]) == (
        "足球資料庫",
        "Soccer Database",
        "足球数据库",
    )
    assert title["zh_hant"] == "使用者自訂標題"

