# HPPV 往績下載（FootyLogic 按年）修正報告

日期：2026-11-16  
Commit／push：未執行（待用戶批准）

## 根因分析

「指定下載賽果年份」原本走 HKJC GraphQL `matchResults`（`match_results_by_year` preset），依日期窗寫入／更新 `soccer_daily`，**不是** FootyLogic 全目錄、全賽事類型的往績下載。

因此無法保證取得 FootyLogic 某一年的聯賽、盃賽及其他賽事，亦會把 HKJC 賽果誤當往績主來源。

## 修改檔案清單

| 檔案 | 變更 |
|---|---|
| `hppv/footylogic_catalog.py` | **新增** 目錄發現（categoryId 1–4；nested + empty group） |
| `hppv/footylogic_history_year.py` | **新增** 按年 seed jobs、下載、完整性報告、背景啟動 |
| `hppv/footylogic_hkjc_overlay.py` | **新增** HKJC 重覆匹配／覆蓋（保留 FL 原文） |
| `hppv/soccer_db.py` | init 時建立 year_runs／overlay schema |
| `hppv/web_app.py` | `/api/soccer/history-year/start|status`；拒絕 by_year GraphQL 當 FL 替代 |
| `hppv/web_templates/soccer_daily.html` | 往績按年 UI（來源／年份／狀態／統計） |
| `hppv/translations.py` | 往績按年文案 |
| `tests/test_footylogic_history_year.py` | **新增** 單元／整合測試 |

## FootyLogic 賽事類型發現及下載流程

1. `GET /tournament/competitions?categoryId=1..4`
   - **1 league**：nested competitions
   - **2 cup**：empty groups → `groupId` 當 `competitionId`
   - **3 international → other**：nested + empty groups
   - **4 other**：empty groups（常與 cup 重覆；以 competitionId 去重，保留先見）
2. 對每個 competition：`GET /tournament/fixtures-seasons`
3. 以 calendar year 過濾 `fixtureRange` 月份 → 建立 `footylogic_jobs`（月切塊）
4. 逐 job：`GET /tournament/fixtures-results` → parse → upsert `footylogic_matches`（`source=footylogic`）
5. 重跑：已 `completed` job 不重抓；upsert 以 `source_key` 幂等
6. 完成判定：`integrity.can_claim_complete` 需目錄成功、四類遍歷、jobs 終態、無失敗／待確認

### 實機目錄探測（本機，2026-11-16）

```
catalog_ok True
competition_count 180
by_type {'league': 133, 'cup': 14, 'other': 33}
cat1 ok nested=133 empty=0
cat2 ok nested=0 empty=14
cat3 ok nested=21 empty=12
cat4 ok nested=0 empty=14（與 cup 去重）
```

樣本 seasons（2025）：英超 10 月、歐冠 11 月、世盃 0 月（該年無 fixtureRange）

## HKJC 重覆賽事匹配及覆蓋規則

穩定條件（全部符合才自動覆蓋）：

- `match_date_hkt` 相同
- `home_team_name` / `away_team_name` 完全相同（不接受主客相反）
- `competition_name` 相同

行為：

- 確認重覆：HKJC 比分／`MATCHVOIDED`（`hkjc_match_voided`）寫入 **FL 列顯示欄**；FL 原文存 `footylogic_hkjc_overlay.fl_original_json`
- FL 獨有：保留，不以 HKJC 補足或刪除
- 不確定（主客相反、賽事名稱不符、多名候選）：標記 `uncertain`，不自動覆蓋
- **不**把 HKJC 獨有賽事納入「FL 往績下載範圍」
- 每日賽事／每日賽果／tombstone 流程未改

## 測試結果

```
pytest tests/test_footylogic_history_year.py tests/test_soccer_task_update_results.py -q
→ 全部通過
```

對照需求：

| # | 項目 | 結果 |
|---|---|---|
| 1 | 年份目錄含聯賽／盃賽／其他 | 通過（單元 + 本機 live 180 項） |
| 2 | 各類型遍歷 | 通過（cat 1–4 ok） |
| 3 | FL 獨有不被覆蓋／刪除 | 通過（overlay 測試） |
| 4 | 重覆以 HKJC 為準 | 通過 |
| 5 | 不同日期／主客相反不誤判 | 通過 |
| 6 | 重跑不重抓 completed | 通過 |
| 7 | 失敗／部分成功不偽裝完成 | 通過（integrity） |
| 8 | 每日仍 HKJC | 通過（update_results 測試仍綠；by_year GraphQL 改拒） |
| 9 | 每日賽果→往績／刪除／tombstone | 未改程式路徑；既有測試通過 |
| 10 | `.185` 實機 | **未完成**（見下） |

## `.185` 實機證據

| 檢查 | 結果 |
|---|---|
| `192.168.1.185:800` | 連線逾時／關閉（Web 服務不可達） |
| `192.168.1.185:445` | TCP open，但先前 SMB 存取被拒 |

因此本次無法在 `.185` 取得執行版本、DB 寫入與完整性報告證據。本機已驗證目錄發現與測試套件。

待 `.185` Web／SMB 可寫入後，建議：

1. 部署本變更後重啟 `python -m hppv.web_app`
2. UI「下載往績（FootyLogic 按年）」選年份 → 開始 → 保存 status JSON
3. SQL：`footylogic_year_runs`、`footylogic_jobs` 狀態分布、`footylogic_hkjc_overlay` 計數

## 完整性報告欄位（API／UI）

- 來源：FootyLogic
- 年份、賽事類型計數（league/cup/other）
- 狀態：未開始／載入中／成功／部分成功／失敗
- Jobs：total／completed／failed／pending／empty_ok
- 場次：downloaded_fl／fl_only／hkjc_overlay_applied／uncertain／skipped
- `integrity.can_claim_complete`：僅在目錄＋遍歷＋終態＋無錯誤／待確認時為 true

## 使用方式

1. 足球 → 下載分頁 → 選「下載往績（FootyLogic 按年）」
2. 輸入年份 →「開始下載往績」或「續跑／重試失敗項」
3. 狀態區顯示完整性報告；背景執行時可「重新整理狀態」
