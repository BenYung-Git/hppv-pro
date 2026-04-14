
from flask import Flask, render_template, request
from hppv.config import default_config
from hppv import db, scraper

app = Flask(__name__)
cfg = default_config()

@app.route("/", methods=["GET", "POST"])
def index():
    records = []
    horse_id = ""
    if request.method == "POST":
        horse_id = request.form.get("horse_id", "").strip()
        if horse_id:
            # 先抓並寫入 DB
            result = scraper.scrape_horse_past_performance(
                horse_id, cfg.hkjc_horse_url_template
            )
            db.upsert_race_records(cfg.db_path, result.records)
            # 再從 DB 讀出顯示
            records = db.get_history_by_horse_id(cfg.db_path, horse_id)
    return render_template("index.html", horse_id=horse_id, records=records)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
