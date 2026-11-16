"""FootyLogic year history download: full catalog → monthly jobs → HKJC duplicate skip."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hppv.footylogic_catalog import (
    ALL_CATEGORY_IDS,
    CatalogDiscoveryResult,
    DiscoveredCompetition,
    discover_competitions,
    fetch_fixture_seasons,
    months_for_year,
)
from hppv.footylogic_client import FootylogicClient, FootylogicClientError, default_fixtures_results_params
from hppv.footylogic_hkjc_overlay import (
    ensure_overlay_schema,
    filter_fl_rows_for_hkjc_skip,
    load_hkjc_by_date_for_year,
    run_hkjc_duplicate_skip_audit_for_year,
)
from hppv.footylogic_ingest import (
    ensure_footylogic_schema,
    list_jobs,
    seed_jobs,
)
from hppv.footylogic_schema import FOOTYLOGIC_JOBS_TABLE, FOOTYLOGIC_MATCHES_TABLE
from hppv.soccer_db import get_connection
from hppv.soccer_task_scheduler import soccer_shared_job_lock

logger = logging.getLogger(__name__)

YEAR_RUNS_TABLE = "footylogic_year_runs"

STATUS_NOT_STARTED = "not_started"
STATUS_LOADING = "loading"
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial_success"
STATUS_FAILED = "failed"

_run_lock = threading.RLock()
_active_thread: threading.Thread | None = None
_memory_status: dict[str, Any] = {
    "status": STATUS_NOT_STARTED,
    "run_id": None,
    "year": None,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_year_runs_schema(conn: Any) -> None:
    ensure_footylogic_schema(conn)
    ensure_overlay_schema(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {YEAR_RUNS_TABLE} (
            run_id TEXT PRIMARY KEY NOT NULL,
            year INTEGER NOT NULL,
            status TEXT NOT NULL,
            report_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_fl_year_runs_year
            ON {YEAR_RUNS_TABLE}(year, updated_at)
        """
    )


def validate_year(year: int | str) -> int:
    y = int(year)
    if y < 1990 or y > 2100:
        raise ValueError("year_out_of_range")
    return y


def _empty_report(year: int) -> dict[str, Any]:
    return {
        "source": "FootyLogic",
        "year": year,
        "status": STATUS_NOT_STARTED,
        "competition_types": ["league", "cup", "other"],
        "catalog": {
            "ok": False,
            "competition_count": 0,
            "by_type": {"league": 0, "cup": 0, "other": 0},
            "by_category": {},
            "errors": [],
            "empty_directories": [],
        },
        "jobs": {
            "seeded": 0,
            "total": 0,
            "pending": 0,
            "completed": 0,
            "failed": 0,
            "empty_ok": 0,
            "skipped_already_completed": 0,
        },
        "download": {
            "attempted": 0,
            "completed": 0,
            "failed": 0,
            "empty_ok": 0,
            "upserted": 0,
            "quarantined": 0,
        },
        "matches": {
            "downloaded_fl": 0,
            "fl_only_written": 0,
            "hkjc_duplicate_skip": 0,
            "uncertain": 0,
            "upserted": 0,
            "upsert_existing": 0,
            "other_skipped": 0,
            # legacy aliases kept for older UI/report readers
            "fl_only": 0,
            "hkjc_overlay_applied": 0,
            "skipped": 0,
        },
        "per_competition": [],
        "failures": [],
        "integrity": {
            "catalog_complete": False,
            "all_types_traversed": False,
            "all_jobs_terminal": False,
            "no_unhandled_errors": False,
            "can_claim_complete": False,
        },
        "message": "",
        "updated_at": _utc_now(),
    }


def _save_run(conn: Any, run_id: str, year: int, status: str, report: dict[str, Any]) -> None:
    ensure_year_runs_schema(conn)
    now = _utc_now()
    report["status"] = status
    report["updated_at"] = now
    payload = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    conn.execute(
        f"""
        INSERT INTO {YEAR_RUNS_TABLE} (run_id, year, status, report_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            status=excluded.status,
            report_json=excluded.report_json,
            updated_at=excluded.updated_at
        """,
        (run_id, int(year), status, payload, now, now),
    )


def _load_latest_run(conn: Any, year: int | None = None) -> dict[str, Any] | None:
    ensure_year_runs_schema(conn)
    if year is None:
        row = conn.execute(
            f"""
            SELECT run_id, year, status, report_json, updated_at
              FROM {YEAR_RUNS_TABLE}
             ORDER BY updated_at DESC
             LIMIT 1
            """
        ).fetchone()
    else:
        row = conn.execute(
            f"""
            SELECT run_id, year, status, report_json, updated_at
              FROM {YEAR_RUNS_TABLE}
             WHERE year=?
             ORDER BY updated_at DESC
             LIMIT 1
            """,
            (int(year),),
        ).fetchone()
    if not row:
        return None
    try:
        report = json.loads(row["report_json"])
    except json.JSONDecodeError:
        report = _empty_report(int(row["year"]))
        report["message"] = "report_json_corrupt"
    report["run_id"] = row["run_id"]
    report["status"] = row["status"]
    report["year"] = int(row["year"])
    return report


def build_year_jobs(
    competitions: list[DiscoveredCompetition],
    seasons_by_comp: dict[str, list[dict[str, Any]]],
    year: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build monthly job dicts for seed_jobs + per-competition summary rows."""
    jobs: list[dict[str, Any]] = []
    per_comp: list[dict[str, Any]] = []
    for comp in competitions:
        seasons = seasons_by_comp.get(comp.competition_id) or []
        months: list[tuple[str, str, str]] = []  # month, seasonId, seasonName
        for s in seasons:
            for m in months_for_year(list(s.get("fixtureRange") or []), year):
                months.append((m, str(s["seasonId"]), str(s.get("seasonName") or "")))
        months_sorted = sorted(set(months), key=lambda x: x[0])
        job_keys: list[str] = []
        for month, sid, sname in months_sorted:
            job_key = f"{comp.competition_id}|{sid}|{month}"
            job_keys.append(job_key)
            jobs.append(
                {
                    "job_key": job_key,
                    "competitionId": comp.competition_id,
                    "competitionName": comp.competition_name,
                    "seasonId": sid,
                    "seasonName": sname,
                    "month": month,
                    "browse_category": comp.browse_type,
                    "country_or_group": comp.group_name,
                }
            )
        per_comp.append(
            {
                "competition_id": comp.competition_id,
                "competition_name": comp.competition_name,
                "browse_type": comp.browse_type,
                "browse_category_id": comp.browse_category_id,
                "kind": comp.kind,
                "seasons_ok": comp.competition_id in seasons_by_comp,
                "season_count": len(seasons),
                "months": [m for m, _, _ in months_sorted],
                "job_count": len(job_keys),
                "job_keys": job_keys,
                "status": "pending",
                "error": None,
            }
        )
    return jobs, per_comp


def _job_status_counts(conn: Any, job_keys: list[str]) -> dict[str, int]:
    if not job_keys:
        return {
            "total": 0,
            "pending": 0,
            "completed": 0,
            "failed": 0,
            "running": 0,
            "other": 0,
        }
    # Chunk IN clause
    counts = {
        "total": len(job_keys),
        "pending": 0,
        "completed": 0,
        "failed": 0,
        "running": 0,
        "other": 0,
    }
    chunk = 400
    for i in range(0, len(job_keys), chunk):
        part = job_keys[i : i + chunk]
        ph = ",".join("?" * len(part))
        rows = conn.execute(
            f"""
            SELECT status, COUNT(*) AS c
              FROM {FOOTYLOGIC_JOBS_TABLE}
             WHERE job_key IN ({ph})
             GROUP BY status
            """,
            part,
        ).fetchall()
        known = 0
        for r in rows:
            st = str(r["status"] or "")
            n = int(r["c"] or 0)
            known += n
            if st in counts:
                counts[st] += n
            elif st == "dry_run":
                counts["completed"] += n
            else:
                counts["other"] += n
    return counts


def _count_fl_matches_for_year(conn: Any, year: int) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS c FROM {FOOTYLOGIC_MATCHES_TABLE}
         WHERE source='footylogic' AND match_date_hkt LIKE ?
        """,
        (f"{int(year):04d}-%",),
    ).fetchone()
    return int(row["c"] or 0) if row else 0


def _compute_integrity(report: dict[str, Any]) -> dict[str, bool]:
    catalog_ok = bool(report.get("catalog", {}).get("ok"))
    by_cat = report.get("catalog", {}).get("by_category") or {}
    all_types = True
    for cid in ALL_CATEGORY_IDS:
        entry = by_cat.get(cid) or by_cat.get(str(cid))
        if not entry or not entry.get("ok"):
            all_types = False
            break
    jobs = report.get("jobs") or {}
    terminal = (
        int(jobs.get("total") or 0) > 0
        and int(jobs.get("pending") or 0) == 0
        and int(jobs.get("failed") or 0) == 0
        and int(jobs.get("running") or 0) == 0
    ) or (
        # Zero jobs after successful catalog may be legitimate (no months in year)
        catalog_ok
        and int(jobs.get("total") or 0) == 0
        and not report.get("failures")
    )
    uncertain = int((report.get("matches") or {}).get("uncertain") or 0)
    no_errors = not report.get("failures") and catalog_ok and uncertain == 0
    can_complete = catalog_ok and all_types and terminal and no_errors
    return {
        "catalog_complete": catalog_ok,
        "all_types_traversed": all_types,
        "all_jobs_terminal": bool(terminal),
        "no_unhandled_errors": bool(no_errors),
        "can_claim_complete": bool(can_complete),
    }


def _finalize_status(report: dict[str, Any]) -> str:
    integrity = _compute_integrity(report)
    report["integrity"] = integrity
    if integrity["can_claim_complete"]:
        return STATUS_SUCCESS
    jobs = report.get("jobs") or {}
    uncertain = int((report.get("matches") or {}).get("uncertain") or 0)
    if report.get("catalog", {}).get("ok") is False:
        return STATUS_FAILED
    if int(jobs.get("failed") or 0) > 0 and int(jobs.get("completed") or 0) == 0:
        return STATUS_FAILED
    # Catalog ok but not fully complete → partial (includes uncertain matches).
    if report.get("catalog", {}).get("ok") and (
        int(jobs.get("completed") or 0) > 0
        or int(jobs.get("total") or 0) == 0
        or uncertain > 0
        or report.get("failures")
    ):
        return STATUS_PARTIAL
    return STATUS_PARTIAL


def discover_and_seed_year(
    db_path: Path,
    year: int,
    *,
    client: FootylogicClient | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Discover full FL catalog and seed monthly jobs for calendar year."""
    client = client or FootylogicClient()
    run_id = run_id or str(uuid.uuid4())
    year = validate_year(year)
    report = _empty_report(year)
    report["run_id"] = run_id
    report["status"] = STATUS_LOADING
    report["message"] = "discovering_catalog"

    with get_connection(db_path) as conn:
        _save_run(conn, run_id, year, STATUS_LOADING, report)
        conn.commit()

    catalog: CatalogDiscoveryResult = discover_competitions(client)
    by_type = {"league": 0, "cup": 0, "other": 0}
    empty_dirs: list[str] = []
    by_category_public: dict[str, Any] = {}
    for cid, cat in catalog.by_category.items():
        by_category_public[str(cid)] = {
            "category_id": cat.category_id,
            "browse_type": cat.browse_type,
            "ok": cat.ok,
            "error": cat.error,
            "groups": cat.groups,
            "nested_count": cat.nested_count,
            "empty_group_count": cat.empty_group_count,
            "competition_count": len(cat.competitions),
            "empty_directory": cat.empty_directory,
        }
        if cat.empty_directory and cat.ok:
            empty_dirs.append(f"category_{cid}")
        if cat.ok:
            for c in cat.competitions:
                # count before dedupe for type stats — use catalog.competitions instead
                pass
    for c in catalog.competitions:
        by_type[c.browse_type] = by_type.get(c.browse_type, 0) + 1

    report["catalog"] = {
        "ok": catalog.ok,
        "competition_count": len(catalog.competitions),
        "by_type": by_type,
        "by_category": by_category_public,
        "errors": list(catalog.errors),
        "empty_directories": empty_dirs,
    }
    if not catalog.ok:
        report["failures"] = list(catalog.errors) or ["catalog_discovery_failed"]
        report["message"] = "catalog_discovery_failed"
        status = STATUS_FAILED
        report["integrity"] = _compute_integrity(report)
        with get_connection(db_path) as conn:
            _save_run(conn, run_id, year, status, report)
            conn.commit()
        return report

    seasons_by_comp: dict[str, list[dict[str, Any]]] = {}
    season_failures: list[str] = []
    per_comp_errors: dict[str, str] = {}
    for comp in catalog.competitions:
        ok, seasons, err = fetch_fixture_seasons(client, comp.competition_id)
        if not ok:
            msg = f"seasons_failed:{comp.competition_id}:{err}"
            season_failures.append(msg)
            per_comp_errors[comp.competition_id] = err or "seasons_failed"
            # Do not invent empty seasons as success — leave missing.
            continue
        seasons_by_comp[comp.competition_id] = seasons

    jobs, per_comp = build_year_jobs(catalog.competitions, seasons_by_comp, year)
    for row in per_comp:
        cid = row["competition_id"]
        if cid in per_comp_errors:
            row["status"] = "failed"
            row["error"] = per_comp_errors[cid]
        elif row["job_count"] == 0:
            row["status"] = "empty_year"
        else:
            row["status"] = "seeded"
    report["per_competition"] = per_comp
    if season_failures:
        report["failures"].extend(season_failures)
        report["message"] = "partial_seasons_discovery"

    with get_connection(db_path) as conn:
        ensure_year_runs_schema(conn)
        inserted = seed_jobs(conn, jobs) if jobs else 0
        # Count already-completed among our keys
        job_keys = [j["job_key"] for j in jobs]
        jcounts = _job_status_counts(conn, job_keys)
        skipped_done = jcounts["completed"]
        report["jobs"] = {
            "seeded": inserted,
            "total": len(job_keys),
            "pending": jcounts["pending"],
            "completed": jcounts["completed"],
            "failed": jcounts["failed"],
            "running": jcounts["running"],
            "empty_ok": 0,
            "skipped_already_completed": skipped_done,
            "job_keys": job_keys,
        }
        report["matches"]["downloaded_fl"] = _count_fl_matches_for_year(conn, year)
        report["message"] = "catalog_seeded"
        status = STATUS_LOADING
        report["integrity"] = _compute_integrity(report)
        _save_run(conn, run_id, year, status, report)
        conn.commit()
    return report


def _filter_jobs_for_keys(conn: Any, job_keys: set[str], limit: int | None) -> list[Any]:
    rows = list_jobs(conn, statuses=("pending", "failed"), limit=None)
    selected = [r for r in rows if str(r["job_key"]) in job_keys]
    selected.sort(key=lambda r: (str(r["month"]), str(r["competition_id"]), str(r["season_id"])))
    if limit is not None:
        return selected[: int(limit)]
    return selected


def run_year_download(
    db_path: Path,
    year: int,
    *,
    client: FootylogicClient | None = None,
    run_id: str | None = None,
    limit: int | None = None,
    skip_discover: bool = False,
    stop_flag: Path | None = None,
) -> dict[str, Any]:
    """
    Full year pipeline: discover (unless skip) → download pending jobs → HKJC overlay.
    Resume-safe: completed jobs are not re-fetched; upserts are idempotent.
    """
    client = client or FootylogicClient()
    year = validate_year(year)

    if skip_discover and run_id:
        with get_connection(db_path) as conn:
            ensure_year_runs_schema(conn)
            row = conn.execute(
                f"SELECT report_json FROM {YEAR_RUNS_TABLE} WHERE run_id=?",
                (run_id,),
            ).fetchone()
            report = json.loads(row["report_json"]) if row else _empty_report(year)
            report["run_id"] = run_id
    else:
        report = discover_and_seed_year(
            db_path, year, client=client, run_id=run_id
        )
        run_id = str(report.get("run_id"))

    if report.get("status") == STATUS_FAILED and not report.get("catalog", {}).get("ok"):
        return report

    job_keys = list((report.get("jobs") or {}).get("job_keys") or [])
    job_key_set = set(job_keys)

    # Custom runner: only year job keys (reuse download logic via temporary status filter)
    # Monkey-patch approach avoided — call internal loop similar to run_jobs.
    download_stats = {
        "attempted": 0,
        "completed": 0,
        "failed": 0,
        "empty_ok": 0,
        "upserted": 0,
        "quarantined": 0,
        "stopped": 0,
        "hkjc_duplicate_skip": 0,
        "uncertain": 0,
        "fl_only": 0,
        "upsert_existing": 0,
    }

    report["status"] = STATUS_LOADING
    report["message"] = "downloading"
    with get_connection(db_path) as conn:
        _save_run(conn, str(run_id), year, STATUS_LOADING, report)
        conn.commit()

    from hppv.footylogic_ingest import (
        insert_quarantine,
        mark_job_finished,
        mark_job_started,
        store_raw_month,
        upsert_matches,
    )
    from hppv.footylogic_parse import parse_fixtures_results_payload
    from hppv.footylogic_schema import FOOTYLOGIC_MATCHES_TABLE as _FL_MATCHES

    with get_connection(db_path) as conn:
        ensure_footylogic_schema(conn)
        ensure_overlay_schema(conn)
        jobs = _filter_jobs_for_keys(conn, job_key_set, limit)
        hkjc_by_date = load_hkjc_by_date_for_year(conn, year=year)
        conn.commit()

    for job in jobs:
        if stop_flag is not None and stop_flag.exists():
            download_stats["stopped"] = 1
            break
        job_key = str(job["job_key"])
        competition_id = str(job["competition_id"])
        season_id = str(job["season_id"])
        month = str(job["month"])
        download_stats["attempted"] += 1

        with get_connection(db_path) as conn:
            mark_job_started(conn, job_key)
            conn.commit()

        params = default_fixtures_results_params(
            competition_id=competition_id,
            season_id=season_id,
            month=month,
        )
        try:
            result = client.get_json("/tournament/fixtures-results", params)
        except FootylogicClientError as e:
            with get_connection(db_path) as conn:
                mark_job_finished(
                    conn,
                    job_key,
                    status="failed",
                    http_status=e.http_status,
                    last_error=f"{e.code}:{e.detail}"[:500],
                )
                conn.commit()
            download_stats["failed"] += 1
            report.setdefault("failures", []).append(f"job_failed:{job_key}:{e.code}")
            continue

        if not result.business_ok or result.body is None:
            with get_connection(db_path) as conn:
                store_raw_month(
                    conn,
                    job_key=job_key,
                    competition_id=competition_id,
                    season_id=season_id,
                    month=month,
                    http_status=result.http_status,
                    business_ok=False,
                    message=result.message,
                    raw_json=result.raw_text,
                )
                mark_job_finished(
                    conn,
                    job_key,
                    status="failed",
                    http_status=result.http_status,
                    last_error=f"business_error:{result.message}"[:500],
                )
                conn.commit()
            download_stats["failed"] += 1
            report.setdefault("failures", []).append(f"job_business:{job_key}")
            continue

        try:
            accepted, quarantined, pstats = parse_fixtures_results_payload(
                result.body,
                competition_id=competition_id,
                season_id=season_id,
                month=month,
            )
        except Exception as e:  # noqa: BLE001
            with get_connection(db_path) as conn:
                store_raw_month(
                    conn,
                    job_key=job_key,
                    competition_id=competition_id,
                    season_id=season_id,
                    month=month,
                    http_status=result.http_status,
                    business_ok=True,
                    message=result.message,
                    raw_json=result.raw_text,
                )
                mark_job_finished(
                    conn,
                    job_key,
                    status="failed",
                    http_status=result.http_status,
                    last_error=f"parse_error:{e}"[:500],
                )
                conn.commit()
            download_stats["failed"] += 1
            report.setdefault("failures", []).append(f"job_parse:{job_key}")
            continue

        try:
            with get_connection(db_path) as conn:
                store_raw_month(
                    conn,
                    job_key=job_key,
                    competition_id=competition_id,
                    season_id=season_id,
                    month=month,
                    http_status=result.http_status,
                    business_ok=True,
                    message=result.message,
                    raw_json=result.raw_text,
                )
                # Refresh HKJC index occasionally is unnecessary within one run;
                # use snapshot from start (HKJC daily flow is separate).
                to_upsert, skip_stats = filter_fl_rows_for_hkjc_skip(
                    conn, accepted, hkjc_by_date=hkjc_by_date
                )
                existing_before = 0
                if to_upsert:
                    keys = [str(r.source_key) for r in to_upsert]
                    chunk = 400
                    for i in range(0, len(keys), chunk):
                        part = keys[i : i + chunk]
                        ph = ",".join("?" * len(part))
                        existing_before += int(
                            conn.execute(
                                f"SELECT COUNT(*) AS c FROM {_FL_MATCHES} "
                                f"WHERE source_key IN ({ph})",
                                part,
                            ).fetchone()["c"]
                            or 0
                        )
                upserted = upsert_matches(conn, to_upsert) if to_upsert else 0
                qn = insert_quarantine(conn, quarantined, job_key=job_key)
                mark_job_finished(
                    conn,
                    job_key,
                    status="completed",
                    http_status=result.http_status,
                    events_raw=pstats["events_raw"],
                    in_range=pstats["in_range"],
                    upserted=upserted,
                    quarantined=qn,
                )
                conn.commit()
            download_stats["completed"] += 1
            download_stats["upserted"] += upserted
            download_stats["quarantined"] += qn
            download_stats["hkjc_duplicate_skip"] += int(
                skip_stats.get("hkjc_duplicate_skip") or 0
            )
            download_stats["uncertain"] += int(skip_stats.get("uncertain") or 0)
            download_stats["fl_only"] += int(skip_stats.get("fl_only") or 0)
            download_stats["upsert_existing"] += existing_before
            if pstats["events_raw"] == 0:
                download_stats["empty_ok"] += 1
        except Exception as e:  # noqa: BLE001
            with get_connection(db_path) as conn:
                mark_job_finished(
                    conn,
                    job_key,
                    status="failed",
                    http_status=result.http_status,
                    last_error=f"db_error:{e}"[:500],
                )
                conn.commit()
            download_stats["failed"] += 1
            report.setdefault("failures", []).append(f"job_db:{job_key}")

    report["download"] = download_stats

    # Post-pass: record decisions for any already-stored FL rows; never overlay.
    try:
        with get_connection(db_path) as conn:
            ensure_year_runs_schema(conn)
            audit = run_hkjc_duplicate_skip_audit_for_year(conn, year=year)
            jcounts = _job_status_counts(conn, job_keys)
            report["jobs"].update(
                {
                    "total": len(job_keys),
                    "pending": jcounts["pending"],
                    "completed": jcounts["completed"],
                    "failed": jcounts["failed"],
                    "running": jcounts["running"],
                    "empty_ok": download_stats["empty_ok"],
                }
            )
            hkjc_skip = int(download_stats.get("hkjc_duplicate_skip") or 0)
            uncertain = int(download_stats.get("uncertain") or 0)
            fl_only = int(download_stats.get("fl_only") or 0)
            # Prefer download-pass counts; audit supplements for pre-existing rows.
            report["matches"] = {
                "downloaded_fl": _count_fl_matches_for_year(conn, year),
                "fl_only_written": fl_only,
                "hkjc_duplicate_skip": hkjc_skip,
                "uncertain": max(uncertain, int(audit.get("uncertain") or 0)),
                "upserted": int(download_stats.get("upserted") or 0),
                "upsert_existing": int(download_stats.get("upsert_existing") or 0),
                "other_skipped": 0,
                "fl_only": fl_only,
                "hkjc_overlay_applied": 0,
                "skipped": hkjc_skip,
                "audit_hkjc_duplicate_skip": int(audit.get("hkjc_duplicate_skip") or 0),
                "audit_fl_only": int(audit.get("fl_only") or 0),
            }
            if report["matches"]["uncertain"]:
                report["message"] = "has_uncertain_matches"
            status = _finalize_status(report)
            if status == STATUS_SUCCESS:
                report["message"] = "complete"
            elif not report.get("message"):
                report["message"] = status
            _save_run(conn, str(run_id), year, status, report)
            conn.commit()
            report["status"] = status
    except Exception as e:  # noqa: BLE001
        logger.exception("year hkjc-skip audit failed: %s", e)
        report.setdefault("failures", []).append(f"hkjc_skip_audit_error:{e}")
        report["message"] = "hkjc_skip_audit_failed"
        status = STATUS_PARTIAL if download_stats["completed"] else STATUS_FAILED
        report["integrity"] = _compute_integrity(report)
        with get_connection(db_path) as conn:
            _save_run(conn, str(run_id), year, status, report)
            conn.commit()
        report["status"] = status

    return report


def get_year_download_status(
    db_path: Path,
    *,
    year: int | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    with get_connection(db_path) as conn:
        ensure_year_runs_schema(conn)
        if run_id:
            row = conn.execute(
                f"SELECT run_id, year, status, report_json FROM {YEAR_RUNS_TABLE} WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "run_not_found", "status": STATUS_NOT_STARTED}
            report = json.loads(row["report_json"])
            report["run_id"] = row["run_id"]
            report["status"] = row["status"]
            report["year"] = int(row["year"])
            # Refresh live job counts if loading
            job_keys = list((report.get("jobs") or {}).get("job_keys") or [])
            if job_keys:
                jcounts = _job_status_counts(conn, job_keys)
                report["jobs"].update(
                    {
                        "pending": jcounts["pending"],
                        "completed": jcounts["completed"],
                        "failed": jcounts["failed"],
                        "running": jcounts["running"],
                    }
                )
                report["matches"]["downloaded_fl"] = _count_fl_matches_for_year(
                    conn, int(row["year"])
                )
            report["ok"] = True
            with _run_lock:
                report["worker_active"] = bool(
                    _active_thread is not None and _active_thread.is_alive()
                )
            return report
        latest = _load_latest_run(conn, year=year)
        if not latest:
            out = _empty_report(year or 0)
            out["ok"] = True
            out["status"] = STATUS_NOT_STARTED
            with _run_lock:
                out["worker_active"] = bool(
                    _active_thread is not None and _active_thread.is_alive()
                )
            return out
        latest["ok"] = True
        with _run_lock:
            latest["worker_active"] = bool(
                _active_thread is not None and _active_thread.is_alive()
            )
        return latest


def start_year_download_background(
    db_path: Path,
    year: int,
    *,
    resume: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Start year download in a daemon thread; returns immediate status."""
    global _active_thread, _memory_status
    year = validate_year(year)

    with _run_lock:
        if _active_thread is not None and _active_thread.is_alive():
            return {
                "ok": False,
                "error": "already_running",
                "status": get_year_download_status(db_path, year=year),
            }

        run_id = str(uuid.uuid4())
        skip_discover = False
        if resume:
            with get_connection(db_path) as conn:
                ensure_year_runs_schema(conn)
                prev = _load_latest_run(conn, year=year)
            if prev and prev.get("jobs", {}).get("job_keys"):
                run_id = str(prev.get("run_id") or run_id)
                skip_discover = True

        _memory_status = {
            "status": STATUS_LOADING,
            "run_id": run_id,
            "year": year,
        }

        def _worker() -> None:
            try:
                with soccer_shared_job_lock():
                    run_year_download(
                        db_path,
                        year,
                        run_id=run_id,
                        limit=limit,
                        skip_discover=skip_discover,
                    )
            except Exception as e:  # noqa: BLE001
                logger.exception("year download worker failed: %s", e)
                try:
                    with get_connection(db_path) as conn:
                        ensure_year_runs_schema(conn)
                        report = _empty_report(year)
                        report["run_id"] = run_id
                        report["failures"] = [f"worker_error:{e}"]
                        report["message"] = "worker_error"
                        _save_run(conn, run_id, year, STATUS_FAILED, report)
                        conn.commit()
                except Exception:  # noqa: BLE001
                    logger.exception("failed to persist worker error")
            finally:
                with _run_lock:
                    global _active_thread
                    _active_thread = None

        t = threading.Thread(
            target=_worker,
            name=f"fl-history-year-{year}",
            daemon=True,
        )
        _active_thread = t
        t.start()

    return {
        "ok": True,
        "run_id": run_id,
        "year": year,
        "status": STATUS_LOADING,
        "resume": skip_discover,
    }
