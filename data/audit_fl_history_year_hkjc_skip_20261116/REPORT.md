# HPPV FootyLogic 往績：HKJC 重覆改為 Skip

日期：2026-11-16  
Commit／push：未執行（待用戶批准）

## 根因分析

上一版對「日期＋主客＋賽事名稱」可靠匹配的 FL↔HKJC 重覆場，會以 HKJC **覆蓋** FootyLogic 比分／狀態。這與用戶要求不符：重覆場應在下載 FootyLogic 往績時 **skip**，不論 HKJC 賽果為何，均不寫入、不更新、不覆蓋。

## 修改檔案清單

| 檔案 | 變更 |
|---|---|
| `hppv/footylogic_hkjc_overlay.py` | **重寫**：可靠匹配 → `hkjc_duplicate_skip`；移除覆蓋寫入；提供下載前 `filter_fl_rows_for_hkjc_skip` |
| `hppv/footylogic_history_year.py` | 寫入前過濾 HKJC 重覆；統計改為 skip／FL 獨有／待確認；post-pass 僅審計標記、不覆蓋 |
| `hppv/web_templates/soccer_daily.html` | 狀態面板分開顯示 HKJC 重覆 skip／FL 獨有寫入／待確認／upsert |
| `hppv/translations.py` | 文案改為 skip、不覆蓋 |
| `tests/test_footylogic_history_year.py` | 覆蓋改 skip 的測試；NULL 比分不清空；雙 source_key 保留 |

## HKJC 重覆匹配及 skip 規則

可靠匹配（**全部**符合才 skip）：

- `match_date_hkt` 相同
- `home_team_name` / `away_team_name` 完全相同
- `competition_name` 相同
- 恰好一名 HKJC 候選

Skip 定義：

- 不 INSERT／不 UPDATE 該場 FootyLogic
- 不以 HKJC 覆蓋 FL
- 不刪除已有 FL 資料
- 決策寫入 `footylogic_hkjc_overlay`（`status=hkjc_duplicate_skip`）
- 統計計入 `hkjc_duplicate_skip`，**不**計入已下載／已寫入

不自動 skip（保留 FL 並標記 `uncertain`）：

- 主客相反
- 賽事名稱不符／缺賽事名
- 多名候選

FL 獨有（無可靠 HKJC 孪生）：正常 upsert。

## 統計定義（API／UI）

| 欄位 | 意義 |
|---|---|
| `downloaded_fl` | 該年庫內 `source=footylogic` 場次數 |
| `upserted` | 本次實際 upsert 場次 |
| `upsert_existing` | upsert 前已存在的同 `source_key` 數 |
| `fl_only_written` | 無 HKJC 孪生而寫入 |
| `hkjc_duplicate_skip` | 可靠重覆而 skip（非已下載） |
| `uncertain` | 待人工確認（已保留／寫入 FL） |
| `other_skipped` | 非 HKJC 重覆的其他跳過 |
| `hkjc_overlay_applied` | 固定 0（已廢止覆蓋） |

## 測試結果

```
pytest tests/test_footylogic_history_year.py -q  → 通過
pytest tests/test_soccer_task_update_results.py -q → 通過（每日／tombstone 未改）
```

| # | 項目 | 結果 |
|---|---|---|
| 1–2 | 目錄／全類型遍歷 | 既有測試 + 先前 live 探測 |
| 3–4 | 可靠重覆 skip、比分不同仍 skip | 通過 |
| 5 | FL 獨有寫入 | 通過 |
| 6 | 不確定不自動 skip | 通過 |
| 7–8 | 同 source_key upsert；NULL 不清舊比分 | 通過 |
| 9 | 不同 source_key 兩筆保留 | 通過 |
| 10–11 | 重跑／失敗完整性 | 既有測試 |
| 12 | 每日流程 | update_results 測試通過 |
| 13 | `.185` | **未完成**（:800 不可達） |

## `.185` 實機

`192.168.1.185:800` 仍不可達；無法交付執行版本／DB skip 統計實機證據。待服務可連後驗證 status JSON 中的 `hkjc_duplicate_skip`／`fl_only_written`。
