"""對賽往績 preset：內建 default + 多方案 JSON（pp_preset_list.json），並保留單檔 pp_preset_default.json 同步。"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

PP_PRESET_LIST_PATH = Path(__file__).resolve().with_name("pp_preset_list.json")
PP_LEGACY_DEFAULT_PATH = Path(__file__).resolve().with_name("pp_preset_default.json")

_BUILTIN: dict[str, Any] = {
    "show_cols": [
        "race_date",
        "distance",
        "draw",
        "going",
        "finish_pos",
        "win_odds",
    ],
    "sort_seq": [
        ("race_date", "desc"),
        ("distance", "asc"),
        ("draw", "asc"),
    ],
}

PP_PRESET_BUILTIN_DEFAULT: dict[str, Any] = {
    "id": "default",
    "name": "系統預設",
    "is_builtin": True,
    "show_cols": list(_BUILTIN["show_cols"]),
    "sort_seq": [tuple(p) for p in _BUILTIN["sort_seq"]],
}

# 向後相容：舊程式碼用 PP_PRESET_DEFAULT 指內建資料形狀
PP_PRESET_DEFAULT: dict[str, Any] = {
    "show_cols": list(_BUILTIN["show_cols"]),
    "sort_seq": [tuple(p) for p in _BUILTIN["sort_seq"]],
}


def _normalize_sort_seq(raw: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            k, d = str(item[0]), str(item[1]).strip().lower()
            out.append((k, d))
    return out


def _preset_from_raw(raw: dict[str, Any]) -> dict[str, Any] | None:
    pid = str(raw.get("id") or "").strip()
    if not pid:
        return None
    name = str(raw.get("name") or pid).strip() or pid
    is_builtin = bool(raw.get("is_builtin", False))
    sc = raw.get("show_cols")
    show_cols = [str(k) for k in sc] if isinstance(sc, list) else []
    sort_seq = _normalize_sort_seq(raw.get("sort_seq"))
    return {
        "id": pid,
        "name": name,
        "is_builtin": is_builtin,
        "show_cols": show_cols,
        "sort_seq": sort_seq,
    }


def _ensure_default_first(presets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not any(p.get("id") == "default" for p in presets):
        presets.insert(0, copy.deepcopy(PP_PRESET_BUILTIN_DEFAULT))
    return presets


def _migrate_legacy_default_file() -> list[dict[str, Any]] | None:
    """若存在舊版單一物件 pp_preset_default.json，轉成一筆 default preset。"""
    if not PP_LEGACY_DEFAULT_PATH.is_file():
        return None
    try:
        raw = json.loads(PP_LEGACY_DEFAULT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(raw, list):
        return None
    if not isinstance(raw, dict):
        return None
    preset = copy.deepcopy(PP_PRESET_BUILTIN_DEFAULT)
    sc = raw.get("show_cols")
    if isinstance(sc, list):
        preset["show_cols"] = [str(k) for k in sc if isinstance(k, str)]
    sq = raw.get("sort_seq")
    if isinstance(sq, list) and sq:
        preset["sort_seq"] = _normalize_sort_seq(sq)
    return [preset]


def load_pp_preset_list() -> list[dict[str, Any]]:
    if PP_PRESET_LIST_PATH.is_file():
        try:
            data = json.loads(PP_PRESET_LIST_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if isinstance(data, list) and data:
            presets: list[dict[str, Any]] = []
            for raw in data:
                if not isinstance(raw, dict):
                    continue
                p = _preset_from_raw(raw)
                if p:
                    presets.append(p)
            presets = _ensure_default_first(presets)
            if presets:
                return presets
    migrated = _migrate_legacy_default_file()
    if migrated is not None:
        save_pp_preset_list(migrated)
        return migrated
    return [copy.deepcopy(PP_PRESET_BUILTIN_DEFAULT)]


def find_preset(presets: list[dict[str, Any]], preset_id: str) -> dict[str, Any] | None:
    for p in presets:
        if p.get("id") == preset_id:
            return p
    return None


def _serialize_presets_for_json(presets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in presets:
        sq = p.get("sort_seq") or []
        seq_json: list[list[str]] = []
        for pair in sq:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                seq_json.append([str(pair[0]), str(pair[1])])
        out.append(
            {
                "id": str(p.get("id") or ""),
                "name": str(p.get("name") or ""),
                "is_builtin": bool(p.get("is_builtin", False)),
                "show_cols": list(p.get("show_cols") or []),
                "sort_seq": seq_json,
            }
        )
    return out


def save_pp_preset_list(presets: list[dict[str, Any]]) -> None:
    tmp = PP_PRESET_LIST_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(_serialize_presets_for_json(presets), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(PP_PRESET_LIST_PATH)


def load_pp_preset_default() -> dict[str, Any]:
    """向後相容：回傳 id=default 之 show_cols / sort_seq（dict）。"""
    presets = load_pp_preset_list()
    d = find_preset(presets, "default") or presets[0]
    return {
        "show_cols": list(d.get("show_cols") or []),
        "sort_seq": [tuple(p) for p in (d.get("sort_seq") or [])],
    }


def save_pp_preset_default(preset: dict[str, Any]) -> None:
    """向後相容：更新 default 方案並同步寫入 pp_preset_default.json。"""
    presets = load_pp_preset_list()
    target = find_preset(presets, "default")
    if target is None:
        presets.insert(0, copy.deepcopy(PP_PRESET_BUILTIN_DEFAULT))
        target = presets[0]
    sc = preset.get("show_cols")
    if isinstance(sc, list):
        target["show_cols"] = [str(k) for k in sc if isinstance(k, str)]
    sq = preset.get("sort_seq")
    if isinstance(sq, list):
        target["sort_seq"] = _normalize_sort_seq(sq)
    save_pp_preset_list(presets)
    legacy = {
        "show_cols": list(target.get("show_cols") or []),
        "sort_seq": [[a, b] for a, b in (target.get("sort_seq") or [])],
    }
    tmp = PP_LEGACY_DEFAULT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(legacy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(PP_LEGACY_DEFAULT_PATH)


def slug_preset_id(name: str, existing: set[str]) -> str:
    base = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip()).strip("_").lower() or "preset"
    if base not in existing:
        return base
    idx = 2
    while f"{base}_{idx}" in existing:
        idx += 1
    return f"{base}_{idx}"
