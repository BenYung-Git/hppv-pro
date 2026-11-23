"""Racing download async jobs + live progress polling."""

from __future__ import annotations

import time
from pathlib import Path

from hppv.db import init_db
from hppv.racing_download_jobs import (
    get_racing_download_job,
    start_racing_download_job,
)
from hppv.racing_local_results import RESULTS_PERIOD_QUERY
from hppv.racing_requests import ensure_racing_preset_files
import json


def test_results_period_job_progress_and_completion(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "hppv.sqlite3"
    init_db(db)
    requests_dir = tmp_path / "racing_requests"
    ensure_racing_preset_files(requests_dir)

    def fake_collect(*, start, end, venue_codes, on_meeting=None, **kwargs):
        metas = []
        for i, venue in enumerate(venue_codes):
            meta = {
                "race_date": start.strftime("%d/%m/%Y"),
                "venue_code": venue,
                "race_nos": [1],
                "races_fetched": 1,
                "races_with_results": 0,
                "record_count": 0,
            }
            metas.append(meta)
            if on_meeting:
                on_meeting(meta)
            time.sleep(0.05)
        return [], [], metas

    monkeypatch.setattr(
        "hppv.racing_download.collect_period_race_records", fake_collect
    )

    payload = {
        "operationName": "localResultsPeriod",
        "query": RESULTS_PERIOD_QUERY,
        "variables": {
            "startDate": "2025-11-19",
            "endDate": "2025-11-19",
            "venueCodes": ["HV", "ST"],
        },
    }
    job = start_racing_download_job(
        db_path=db,
        request_key="results_period",
        raw_payload=json.dumps(payload),
    )
    assert job.job_id
    saw_running = False
    final = None
    for _ in range(100):
        cur = get_racing_download_job(job.job_id)
        assert cur is not None
        pub = cur.to_public()
        if pub.get("job_status") in {"running", "queued"}:
            saw_running = True
            assert "processed" in pub
        if pub.get("job_status") in {"success", "failed"}:
            final = pub
            break
        time.sleep(0.05)
    assert saw_running
    assert final is not None
    assert final["job_status"] == "success"
    assert final.get("ok") is True


def test_racecard_job_rejects_bad_json(tmp_path: Path) -> None:
    db = tmp_path / "hppv.sqlite3"
    init_db(db)
    from hppv.racing_download_jobs import RacingDownloadJobError

    try:
        start_racing_download_job(
            db_path=db, request_key="racecard", raw_payload="{not-json"
        )
        raise AssertionError("expected RacingDownloadJobError")
    except RacingDownloadJobError as e:
        assert e.code == "invalid_json"
