from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import requests


GRAPHQL_URL = "https://info.cld.hkjc.com/graphql/base/"

GRAPHQL_QUERY = """fragment raceFragment on Race {
  id
  no
  raceName_ch
  raceName_en
  postTime
}

query raceMeetings($date: String, $venueCode: String) {
  raceMeetings(date: $date, venueCode: $venueCode) {
    id
    venueCode
    date
    races {
      ...raceFragment
      runners {
        id
        no
        name_ch
        name_en
        winOdds
      }
    }
  }
}
"""


def get_graphql_data(date: str, venue_code: str) -> dict[str, Any]:
    variables = {"date": date, "venueCode": venue_code}
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://bet.hkjc.com",
        "Referer": "https://bet.hkjc.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "zh-HK,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "sec-fetch-site": "same-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
    }

    # Use deterministic JSON serialization to avoid any gateway/hash mismatch.
    payload = {
        "operationName": "raceMeetings",
        "variables": variables,
        "query": GRAPHQL_QUERY,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    resp = requests.post(GRAPHQL_URL, headers=headers, data=body, timeout=20)
    resp.raise_for_status()

    data = resp.json()
    if "errors" in data and data["errors"]:
        # Raise with concise details; keep raw in exception args for debugging.
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    if "data" not in data:
        raise RuntimeError(f"GraphQL response missing 'data': {data}")
    return data["data"]


def update_win_odds(
    conn: sqlite3.Connection,
    race_date: str,
    racecourse: str,
    race_no: int,
    horse_no: str,
    win_odds: Any,
) -> None:
    cur = conn.execute(
        """
        UPDATE racecard_starters
           SET "即時賠率" = ?
         WHERE race_date = ?
           AND racecourse = ?
           AND race_no = ?
           AND "馬號" = ?
           AND 後備 = '否';
        """,
        (win_odds, race_date, racecourse, race_no, horse_no),
    )

    if cur.rowcount == 0:
        # 找不到匹配紀錄就略過（只提示，不新增 row）。
        print(
            f"[略過] 未找到匹配：{race_date} {racecourse} 第{race_no}場 馬號={horse_no} 後備=否"
        )


def main() -> None:
    # Assumption per your spec.
    # Allow override via CLI: python ...\racecard_update_immediate_odds.py path\to\racecard.db
    db_path = Path(sys.argv[1]) if len(sys.argv) >= 2 else Path("racecard.db")
    if not db_path.exists():
        raise FileNotFoundError(f"找不到資料庫檔案：{db_path.resolve()}")

    # Fixed variables per your spec.
    date = "2026-03-22"
    venue_code = "ST"

    # 1) Fetch GraphQL JSON
    try:
        data = get_graphql_data(date=date, venue_code=venue_code)
    except Exception as e:
        print(f"GraphQL 抓取失敗：{e}")
        return

    # Expected shape:
    # data["raceMeetings"] -> list of meetings
    meetings = data.get("raceMeetings") or []
    if not meetings:
        print("GraphQL：找不到 raceMeetings。")
        return

    # 2) Update existing records only
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        # In case the script updates many rows, wrap in a transaction.
        for meeting in meetings:
            meeting_date = meeting.get("date") or date
            meeting_venue = meeting.get("venueCode") or venue_code

            races = meeting.get("races") or []
            for race in races:
                race_no = race.get("no")
                if race_no is None:
                    continue

                runners = race.get("runners") or []
                for runner in runners:
                    horse_no = runner.get("no")
                    if horse_no is None:
                        continue

                    win_odds = runner.get("winOdds")
                    update_win_odds(
                        conn=conn,
                        race_date=str(meeting_date),
                        racecourse=str(meeting_venue),
                        race_no=int(race_no),
                        horse_no=str(horse_no),
                        win_odds=win_odds,
                    )

        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()

