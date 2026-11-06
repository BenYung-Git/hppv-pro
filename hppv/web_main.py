from __future__ import annotations
import logging

import os
from typing import Any

from hppv.config import default_config
from hppv import db, scraper
from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory

# 關掉 werkzeug 的 access log
logging.getLogger("werkzeug").setLevel(logging.WARNING)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
#print(">>> TEMPLATE_DIR =", TEMPLATE_DIR)

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.secret_key = "hppv-demo-secret"

cfg = default_config()


def _race_meeting_snapshot() -> dict[str, Any]:
    """
    讀取最新一個排位賽馬日簡要資料：

    - 賽日／場地：racecard_meta（last_source_race_date / last_source_racecourse_code）
    - 場次：racecard_starters 裡該賽日＋場地的 DISTINCT race_no 個數
    - 已存賽日數：racecard_starters 裡不同 race_date/racecourse 的個數
    """
    race_date = "—"
    racecourse_code = "—"
    race_count = 0
    stored_meetings = 0

    # 1) 用 meta 取日期與場地代碼
    try:
        with db.get_connection(cfg.db_path) as conn:
            conn.row_factory = db.sqlite3.Row
            meta_rows = conn.execute(
                """
                SELECT key, value FROM racecard_meta
                WHERE key IN (
                    'last_source_race_date',
                    'last_source_racecourse_code'
                );
                """
            ).fetchall()
    except Exception:
        meta_rows = []

    meta = {str(r["key"]): str(r["value"]) for r in meta_rows}
    race_date = meta.get("last_source_race_date", "—").strip() or "—"
    racecourse_code = meta.get("last_source_racecourse_code", "—").strip() or "—"

    print(">>> META date/code:", race_date, racecourse_code)

    # 2) 用 racecard_starters 算實際場次 + 已存賽日數
    try:
        with db.get_connection(cfg.db_path) as conn:
            conn.row_factory = db.sqlite3.Row

            if race_date != "—" and racecourse_code != "—":
                # racecard_starters.race_date 這裡也是 "2026/03/25" 格式，所以直接用
                racedate_key = race_date.strip()
                print(">>> QUERY racecard_starters with:", racedate_key, racecourse_code)

                row = conn.execute(
                    """
                    SELECT COUNT(DISTINCT race_no) AS c
                    FROM racecard_starters
                    WHERE race_date = ? AND racecourse = ?;
                    """,
                    (racedate_key, racecourse_code),
                ).fetchone()
                print(">>> ROW race_count query:", row)
                race_count = int(row["c"]) if row else 0

            row2 = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM (
                    SELECT race_date, racecourse
                    FROM racecard_starters
                    GROUP BY race_date, racecourse
                ) x;
                """
            ).fetchone()
            stored_meetings = int(row2["c"]) if row2 else 0
    except Exception as e:
        print(">>> ERROR in snapshot:", e)

    return {
        "race_date": race_date,
        "racecourse": racecourse_code,
        "race_count": race_count,
        "stored_meetings": stored_meetings,
    }






def _racecourse_display(code: str) -> str:
    mapping = {"ST": "沙田", "HV": "跑馬地", "FC": "從化"}
    c = str(code or "").strip().upper()
    return mapping.get(c, c or "—")


def _message_board_images() -> list[str]:
    """讀 assets/message_board 下的圖片檔名，用於 Dashboard 輪播。"""
    assets_dir = os.path.join(BASE_DIR, "assets", "message_board")
    if not os.path.isdir(assets_dir):
        return []
    files = [
        f for f in os.listdir(assets_dir)
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".gif"))
    ]
    files.sort()
    return files

@app.route("/assets/message_board/<path:filename>")
def message_image(filename: str):
    """Serve images from assets/message_board for the dashboard carousel."""
    folder = os.path.join(BASE_DIR, "assets", "message_board")
    return send_from_directory(folder, filename)


@app.route("/")
def dashboard() -> str:
    # 統計：馬數
    try:
        active_total = db.count_horses(cfg.db_path)
    except Exception:
        active_total = 0

    # 統計：race_records
    try:
        with db.get_connection(cfg.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM race_records;").fetchone()
            race_records_total = int(row["c"]) if row else 0
    except Exception:
        race_records_total = 0

    snap = _race_meeting_snapshot()
    #print(">>> SNAP:", snap)

    stats = {
        "active_horses": active_total,
        "race_records_total": race_records_total,
        "race_date": snap["race_date"],                      # 例如 "2026/03/25"
        "racecourse": _racecourse_display(snap["racecourse"]),  # 例如 "跑馬地"
        "race_count": snap["race_count"],                    # 9
        "stored_meetings": snap["stored_meetings"],
    }
    #print(">>> STATS:", stats)

    images = _message_board_images()
    return render_template("dashboard.html", stats=stats, images=images)


@app.route("/horses", methods=["GET", "POST"])
def horses_entry() -> str:
    """
    馬匹／往績入口頁：
    - GET：顯示表單
    - POST：輸入 HorseId -> 抓往績寫入 DB -> redirect 到 /horse/<horse_id>
    """
    horse_id = ""
    if request.method == "POST":
        horse_id = request.form.get("horse_id", "").strip()
        if not horse_id:
            flash("請輸入 HorseId。")
        else:
            try:
                result = scraper.scrape_horse_past_performance(
                    horse_id, cfg.hkjc_horse_url_template
                )
                db.upsert_race_records(cfg.db_path, result.records)
                return redirect(url_for("horse_history", horse_id=horse_id))
            except Exception as e:
                flash(f"抓取往績時發生錯誤：{e}")

    return render_template("web_main.html", horse_id=horse_id)


@app.route("/horse/<horse_id>")
def horse_history(horse_id: str) -> str:
    """顯示指定 HorseId 的歷史賽績（使用 db.fetch_race_records）。"""
    try:
        records = db.fetch_race_records(cfg.db_path, horse_id)
    except Exception:
        records = []

    return render_template("horse_history.html", horse_id=horse_id, records=records)


def main() -> None:
    app.run(host="0.0.0.0", port=800, debug=False)


if __name__ == "__main__":
    main()



