from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .config import default_config
from .db import get_all_horse_results_with_default_sort, init_db


@dataclass
class ReportDefinition:
    code: str
    name: str
    sheet_name: str
    filename_prefix: str

    def generate(self) -> tuple[pd.DataFrame, str]:
        raise NotImplementedError


class HorseResultsReport(ReportDefinition):
    def __init__(self) -> None:
        super().__init__(
            code="horse_results",
            name="馬匹往績報表",
            sheet_name="馬匹往績",
            filename_prefix="馬匹往績",
        )

    def generate(self) -> tuple[pd.DataFrame, str]:
        db_path = default_config().db_path
        init_db(db_path)
        rows = get_all_horse_results_with_default_sort(db_path)
        df = pd.DataFrame(rows)
        return df, self.filename_prefix


_REPORTS: dict[str, ReportDefinition] = {
    "horse_results": HorseResultsReport(),
}


def get_report_definition(code: str) -> ReportDefinition:
    if code not in _REPORTS:
        raise KeyError(code)
    return _REPORTS[code]
