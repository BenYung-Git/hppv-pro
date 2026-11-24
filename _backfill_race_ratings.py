"""One-off: backfill empty race_records.rating / handicap from horse pages.

Does not change product code. Only UPDATE blank fields in place. Resume via JSON.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

from hppv.config import HKJC_HORSE_DETAIL_URL_ZH
from hppv.db import normalize_race_record_date_iso
from hppv.scraper import scrape_horse_past_performance

ISO_SQL = """
CASE
  WHEN race_date LIKE '__/__/__'
  THEN ('20' || substr(race_date, 7, 2) || '-' || substr(race_date, 4, 2) || '-' || substr(race_date, 1, 2))
  WHEN race_date LIKE '__/__/____'
  THEN (substr(race_date, 7, 4) || '-' || substr(race_date, 4, 2) || '-' || substr(race_date, 1, 2))
  WHEN race_date LIKE '____/__/__'
  THEN (substr(race_date, 1, 4) || '-' || substr(race_date, 6, 2) || '-' || substr(race_date, 9, 2))
  WHEN race_date LIKE '____-__-__'
  THEN race_date
  ELSE ''
END
"""


def _norm_race_no(raw: object) -> str:
    s = str(raw or "").strip()
    if s.isdigit():
        return str(int(s))
    return s


def _need_sql(fields: set[str]) -> str:
    parts: list[str] = []
    if "rating" in fields:
        parts.append("(rating IS NULL OR TRIM(rating) = '')")
    if "handicap" in fields:
        parts.append("(handicap IS NULL OR TRIM(handicap) = '')")
    if not parts:
        raise ValueError("fields empty")
    return "(" + " OR ".join(parts) + ")"


def _load_progress(path: Path) -> dict:
    if not path.is_file():
        return {
            "done_horses": [],
            "updated_rating": 0,
            "updated_handicap": 0,
            "errors": [],
        }
    return json.loads(path.read_text(encoding="utf-8"))


def _save_progress(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _print_stats(con: sqlite3.Connection, since: str, need_sql: str) -> None:
    row = con.execute(
        f"""
        SELECT
          SUM(CASE WHEN rating IS NULL OR TRIM(rating)='' THEN 1 ELSE 0 END) AS empty_rating,
          SUM(CASE WHEN handicap IS NULL OR TRIM(handicap)='' THEN 1 ELSE 0 END) AS empty_handicap,
          SUM(CASE WHEN {need_sql} THEN 1 ELSE 0 END) AS need_any,
          COUNT(DISTINCT CASE WHEN {need_sql} THEN horse_id END) AS horses
        FROM race_records
        WHERE ({ISO_SQL}) >= ?
        """,
        (since,),
    ).fetchone()
    print(
        f"stats since={since} empty_rating={row[0]} empty_handicap={row[1]} "
        f"need_any={row[2]} horses={row[3]}",
        flush=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path(r"c:\Hppv\data\hppv.sqlite3"))
    ap.add_argument("--since", default="2026-03-01")
    ap.add_argument(
        "--fields",
        default="rating,handicap",
        help="Comma list: rating and/or handicap",
    )
    ap.add_argument(
        "--progress",
        type=Path,
        default=Path(r"c:\Hppv\data\_backfill_race_ratings_progress.json"),
    )
    ap.add_argument("--sleep", type=float, default=0.35)
    ap.add_argument("--limit-horses", type=int, default=0)
    ap.add_argument("--stats-only", action="store_true")
    args = ap.parse_args()

    fields = {f.strip() for f in str(args.fields).split(",") if f.strip()}
    if not fields <= {"rating", "handicap"}:
        raise SystemExit(f"invalid --fields {args.fields!r}")
    need_sql = _need_sql(fields)

    con = sqlite3.connect(str(args.db))
    con.row_factory = sqlite3.Row
    _print_stats(con, args.since, need_sql)
    if args.stats_only:
        con.close()
        return 0

    progress = _load_progress(args.progress)
    done = set(progress.get("done_horses") or [])
    upd_rating = int(progress.get("updated_rating") or 0)
    upd_handicap = int(progress.get("updated_handicap") or 0)
    errors: list = list(progress.get("errors") or [])

    horse_ids = [
        r["horse_id"]
        for r in con.execute(
            f"""
            SELECT horse_id, COUNT(*) AS c
            FROM race_records
            WHERE {need_sql} AND ({ISO_SQL}) >= ?
            GROUP BY horse_id
            ORDER BY c DESC, horse_id
            """,
            (args.since,),
        )
    ]
    pending = [h for h in horse_ids if h not in done]
    if args.limit_horses > 0:
        pending = pending[: args.limit_horses]

    print(
        f"fields={sorted(fields)} horses_need={len(horse_ids)} "
        f"done={len(done)} pending={len(pending)}",
        flush=True,
    )

    for i, hid in enumerate(pending, start=1):
        try:
            result = scrape_horse_past_performance(
                hid, HKJC_HORSE_DETAIL_URL_ZH, db_path=args.db
            )
            by_key: dict[tuple[str, str], list[tuple[str, str]]] = {}
            for rec in result.records:
                iso = normalize_race_record_date_iso(rec.race_date)
                if not iso or iso < args.since:
                    continue
                rating = str(rec.rating or "").strip()
                handicap = str(rec.handicap or "").strip()
                if "rating" in fields and "handicap" not in fields and not rating:
                    continue
                if "handicap" in fields and "rating" not in fields and not handicap:
                    continue
                if not rating and not handicap:
                    continue
                key = (iso, _norm_race_no(rec.race_no))
                by_key.setdefault(key, []).append((rating, handicap))

            db_rows = con.execute(
                f"""
                SELECT id, race_date, race_no, rating, handicap
                FROM race_records
                WHERE horse_id = ?
                  AND {need_sql}
                  AND ({ISO_SQL}) >= ?
                """,
                (hid, args.since),
            ).fetchall()

            n_r = 0
            n_h = 0
            for row in db_rows:
                iso = normalize_race_record_date_iso(row["race_date"])
                if not iso:
                    continue
                cands = by_key.get((iso, _norm_race_no(row["race_no"]))) or []
                if len(cands) != 1:
                    continue
                rating, handicap = cands[0]
                if (
                    "rating" in fields
                    and not str(row["rating"] or "").strip()
                    and rating
                ):
                    cur = con.execute(
                        """
                        UPDATE race_records SET rating = ?
                        WHERE id = ? AND (rating IS NULL OR TRIM(rating) = '')
                        """,
                        (rating, int(row["id"])),
                    )
                    n_r += int(cur.rowcount or 0)
                if (
                    "handicap" in fields
                    and not str(row["handicap"] or "").strip()
                    and handicap
                ):
                    cur = con.execute(
                        """
                        UPDATE race_records SET handicap = ?
                        WHERE id = ? AND (handicap IS NULL OR TRIM(handicap) = '')
                        """,
                        (handicap, int(row["id"])),
                    )
                    n_h += int(cur.rowcount or 0)
            con.commit()
            upd_rating += n_r
            upd_handicap += n_h
            done.add(hid)
            progress = {
                "done_horses": sorted(done),
                "updated_rating": upd_rating,
                "updated_handicap": upd_handicap,
                "errors": errors[-50:],
                "last_horse": hid,
                "last_rating": n_r,
                "last_handicap": n_h,
                "scraped_records": len(result.records),
                "fields": sorted(fields),
                "since": args.since,
            }
            _save_progress(args.progress, progress)
            print(
                f"[{i}/{len(pending)}] {hid} scrape={len(result.records)} "
                f"rating+={n_r} handicap+={n_h} "
                f"tot_r={upd_rating} tot_h={upd_handicap}",
                flush=True,
            )
        except Exception as e:
            errors.append({"horse_id": hid, "error": str(e)})
            progress = {
                "done_horses": sorted(done),
                "updated_rating": upd_rating,
                "updated_handicap": upd_handicap,
                "errors": errors[-50:],
                "last_horse": hid,
                "last_error": str(e),
                "fields": sorted(fields),
                "since": args.since,
            }
            _save_progress(args.progress, progress)
            print(f"[{i}/{len(pending)}] {hid} ERROR {e}", flush=True)
        time.sleep(max(0.0, float(args.sleep)))

    _print_stats(con, args.since, need_sql)
    print(
        f"DONE updated_rating={upd_rating} updated_handicap={upd_handicap} "
        f"errors={len(errors)}",
        flush=True,
    )
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
