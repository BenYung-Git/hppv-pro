"""Footylogic historical download runner (dry-run / execute / resume)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from hppv.config import default_config
from hppv.footylogic_client import (
    FootylogicClient,
    FootylogicClientError,
    default_fixtures_results_params,
)
from hppv.footylogic_ingest import (
    ensure_footylogic_schema,
    init_footylogic_tables,
    insert_quarantine,
    list_jobs,
    mark_job_finished,
    mark_job_started,
    seed_jobs,
    store_raw_month,
    upsert_matches,
)
from hppv.footylogic_parse import parse_fixtures_results_payload
from hppv.soccer_db import get_connection


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "jobs" not in data:
        raise ValueError("manifest missing jobs")
    return data


def cmd_init_jobs(db_path: Path, manifest_path: Path) -> int:
    manifest = load_manifest(manifest_path)
    init_footylogic_tables(db_path)
    with get_connection(db_path) as conn:
        ensure_footylogic_schema(conn)
        n = seed_jobs(conn, manifest["jobs"])
        conn.commit()
    print(f"db={db_path.resolve()}")
    print(f"jobs_seeded_or_existing_ignored={n}")
    print(f"manifest_jobs={len(manifest['jobs'])}")
    return 0


def cmd_status(db_path: Path) -> int:
    with get_connection(db_path) as conn:
        ensure_footylogic_schema(conn)
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM footylogic_jobs GROUP BY status ORDER BY status"
        ).fetchall()
        matches = conn.execute(
            "SELECT COUNT(*) AS c, SUM(has_score) AS scored FROM footylogic_matches"
        ).fetchone()
        q = conn.execute("SELECT COUNT(*) AS c FROM footylogic_quarantine").fetchone()
    print(f"db={db_path.resolve()}")
    for r in rows:
        print(f"jobs_{r['status']}={r['c']}")
    print(f"matches={matches['c'] if matches else 0}")
    print(f"matches_has_score={matches['scored'] if matches and matches['scored'] is not None else 0}")
    print(f"quarantine={q['c'] if q else 0}")
    return 0


def run_jobs(
    db_path: Path,
    *,
    limit: int | None,
    dry_run: bool,
    client: FootylogicClient | None = None,
    stop_flag: Path | None = None,
) -> dict[str, int]:
    client = client or FootylogicClient()
    stats = {
        "attempted": 0,
        "completed": 0,
        "failed": 0,
        "empty_ok": 0,
        "upserted": 0,
        "quarantined": 0,
        "stopped": 0,
    }
    with get_connection(db_path) as conn:
        ensure_footylogic_schema(conn)
        jobs = list_jobs(conn, limit=limit)
        conn.commit()

    for job in jobs:
        if stop_flag is not None and stop_flag.exists():
            stats["stopped"] = 1
            break
        job_key = str(job["job_key"])
        competition_id = str(job["competition_id"])
        season_id = str(job["season_id"])
        month = str(job["month"])
        stats["attempted"] += 1

        with get_connection(db_path) as conn:
            ensure_footylogic_schema(conn)
            mark_job_started(conn, job_key)
            conn.commit()

        if dry_run:
            with get_connection(db_path) as conn:
                mark_job_finished(
                    conn,
                    job_key,
                    status="dry_run",
                    http_status=None,
                    events_raw=0,
                    in_range=0,
                    upserted=0,
                    quarantined=0,
                )
                conn.commit()
            stats["completed"] += 1
            continue

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
            stats["failed"] += 1
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
            stats["failed"] += 1
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
            stats["failed"] += 1
            continue

        # Transaction: raw + upsert + quarantine + job complete together
        try:
            with get_connection(db_path) as conn:
                ensure_footylogic_schema(conn)
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
                upserted = upsert_matches(conn, accepted)
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
            stats["completed"] += 1
            stats["upserted"] += upserted
            stats["quarantined"] += qn
            if pstats["events_raw"] == 0:
                stats["empty_ok"] += 1
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
            stats["failed"] += 1

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Footylogic historical results downloader")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-jobs", help="Create Footylogic tables and seed jobs from manifest")
    p_init.add_argument("--db-path", type=Path, required=True, help="Target DB (use TEST path)")
    p_init.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Manifest JSON (default: data/footylogic_jobs_manifest.json)",
    )

    p_status = sub.add_parser("status", help="Show job/match counts")
    p_status.add_argument("--db-path", type=Path, required=True)

    p_run = sub.add_parser("run", help="Process pending/failed jobs")
    p_run.add_argument("--db-path", type=Path, required=True)
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument(
        "--stop-file",
        type=Path,
        default=None,
        help="If this file exists, stop safely after current checkpoint",
    )
    p_run.add_argument("--min-interval", type=float, default=1.05)
    p_run.add_argument(
        "--allow-production",
        action="store_true",
        help="Required to run non-dry-run against AppConfig soccer_db_path",
    )

    args = parser.parse_args(argv)
    cfg = default_config()
    manifest = args.manifest if hasattr(args, "manifest") and args.manifest else (
        cfg.data_dir / "footylogic_jobs_manifest.json"
    )

    if args.command == "init-jobs":
        return cmd_init_jobs(args.db_path, manifest)
    if args.command == "status":
        return cmd_status(args.db_path)
    if args.command == "run":
        # Safety: refuse production soccer DB unless explicitly allowed.
        prod = cfg.soccer_db_path.resolve()
        target = args.db_path.resolve()
        if target == prod and not args.dry_run and not args.allow_production:
            print(
                "REFUSED: refusing non-dry-run against production soccer.sqlite3 "
                "without --allow-production. Use a test DB path, or pass the flag "
                "after user gate confirmation.",
                file=sys.stderr,
            )
            return 3
        client = FootylogicClient(min_interval_sec=args.min_interval)
        stats = run_jobs(
            args.db_path,
            limit=args.limit,
            dry_run=args.dry_run,
            client=client,
            stop_flag=args.stop_file,
        )
        for k, v in stats.items():
            print(f"{k}={v}")
        return 0 if stats["failed"] == 0 else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
