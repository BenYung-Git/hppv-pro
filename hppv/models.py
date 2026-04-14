from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RaceRecord:
    """One past performance row for a horse."""

    horse_id: str
    race_date: str
    venue: str
    race_no: str
    course: str
    distance: str
    going: str
    class_band: str
    draw: str
    rating: str
    jockey: str
    trainer: str
    finish_pos: str
    running_pos: str
    win_odds: str
    time: str
    prize: str

