"""Racing task: 馬匹資料 keys / scope constants (no scheduler import; avoids tzdata)."""

from __future__ import annotations

from pathlib import Path

from hppv.racing_requests import REQUEST_KEY_HORSES, RACING_REQUEST_PRESETS


def test_horses_request_key_registered() -> None:
    assert REQUEST_KEY_HORSES == "horses"
    assert REQUEST_KEY_HORSES in RACING_REQUEST_PRESETS


def test_horses_task_key_present_in_scheduler_module() -> None:
    # Read source to avoid importing soccer_task_scheduler (ZoneInfo/tzdata).
    text = Path("hppv/racing_task_scheduler.py").read_text(encoding="utf-8")
    assert 'TASK_KEY_UPDATE_HORSES = "racing_update_horses"' in text
    assert "UpdateHorsesScheduler" in text
    assert "run_update_horses_once" in text
    assert "insert_or_fill_blanks" in text
    assert "REQUEST_KEY_HORSES" in text
