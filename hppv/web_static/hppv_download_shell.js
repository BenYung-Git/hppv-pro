/**
 * Shared download shell for Soccer + Racing:
 *   request type → load preset → Request box → Send → Status
 *   + task list GET/PUT poll
 *
 * Domain pages only pass config / callbacks; do not fork this flow.
 */
(function (global) {
  "use strict";

  function $(id) {
    return document.getElementById(id);
  }

  function updateSyncBadge(valueId, text, neverLabel) {
    const el = $(valueId);
    if (!el) return;
    el.textContent = text || neverLabel || "—";
  }

  function _ingestReason(ing, MSG) {
    if (!ing) return MSG.ingestSkipped || "";
    const code = ing.code || "";
    if (ing.status === "success" && code === "success") return MSG.ingestSuccess || "";
    if (code === "skipped_empty") return MSG.ingestEmpty || "";
    if (code === "skipped_no_existing") return MSG.ingestNoExisting || "";
    if (code === "truncated") return MSG.ingestTruncated || "";
    if (code === "structure_mismatch") return MSG.ingestStructure || "";
    if (
      code === "graphql_errors" ||
      code === "partial_errors" ||
      code === "http_error" ||
      code === "non_json"
    ) {
      return MSG.ingestGraphqlErrors || "";
    }
    if (
      code === "invalid_match" ||
      code === "duplicate_id" ||
      code === "had_ambiguous" ||
      code === "invalid_result" ||
      code === "result_conflict" ||
      code === "incomplete_results"
    ) {
      return MSG.ingestValidation || "";
    }
    if (code === "table_missing") return MSG.ingestInitRequired || "";
    if (ing.status === "failed") return MSG.ingestFailed || "";
    return (MSG.ingestSkipped || "") + (code ? "（" + code + "）" : "");
  }

  function _resultsCompletenessLabel(ing, vars, MSG) {
    if (!ing) return MSG.ingestCompletenessIncomplete || "";
    if (ing.code === "incomplete_results" || ing.code === "truncated" || ing.status === "failed") {
      return MSG.ingestCompletenessIncomplete || "";
    }
    const endIndex =
      vars && Object.prototype.hasOwnProperty.call(vars, "endIndex") ? vars.endIndex : null;
    if (endIndex === 0) {
      if (
        ing.status === "success" &&
        ing.code === "success" &&
        ing.matches_returned != null &&
        ing.match_num_total != null &&
        ing.matches_returned === ing.match_num_total
      ) {
        return MSG.ingestCompletenessFull || "";
      }
      return MSG.ingestCompletenessIncomplete || "";
    }
    if (typeof endIndex === "number" && endIndex >= 1 && endIndex <= 20) {
      return MSG.ingestCompletenessPage || "";
    }
    return MSG.ingestCompletenessIncomplete || "";
  }

  /**
   * Shared status for unit=record (racing). Never invent totals or percentages.
   */
  function formatRecordDownloadStatus(data, MSG) {
    MSG = MSG || {};
    if (!data) return MSG.error || "";
    if (data.ok === false && data.error && !data.ingest) {
      return (
        (MSG.error || "") +
        "：" +
        data.error +
        (data.detail ? " — " + data.detail : "")
      );
    }
    const ing = data.ingest || {};
    const total = data.total != null ? data.total : ing.total;
    const processed =
      (data.processed != null ? data.processed : ing.processed) || 0;
    const totalDisp = total == null ? "—" : String(total);
    const lines = [
      data.outcome === "success"
        ? MSG.sendComplete || ""
        : MSG.error || "",
      (MSG.ingestProcessed || "") + " " + processed + "/" + totalDisp + " record",
      (MSG.ingestProcessed || "") + "：" + processed,
      (MSG.updated || "") +
        "：" +
        ((data.updated != null ? data.updated : ing.updated) || 0),
      (MSG.skipped || "") +
        "：" +
        ((data.skipped != null ? data.skipped : ing.skipped) || 0),
      (MSG.failed || "") +
        "：" +
        ((data.failed != null ? data.failed : ing.failed) || 0),
    ];
    if (data.race_date) lines.push("race_date：" + data.race_date);
    if (data.venue_code) lines.push("venue：" + data.venue_code);
    const detail = data.detail || ing.detail || {};
    if (detail && detail.operation) lines.push("operation：" + detail.operation);
    if (detail && detail.odds_path) lines.push("odds_path：" + detail.odds_path);
    if (data.error) lines.push("error：" + data.error);
    return lines.join("\n");
  }

  /**
   * Shared soccer GraphQL Status formatter (content fields differ by request_key).
   * ctx.getRequestVariables optional: () => variables from Request box.
   */
  function formatSoccerGraphqlStatus(data, MSG, ctx) {
    MSG = MSG || {};
    ctx = ctx || {};
    if (!data) return MSG.error || MSG.failed || "";
    if (data.ok === false) {
      const code = data.error || "read_failed";
      const mapped =
        (MSG.sendErrors && MSG.sendErrors[code]) || MSG.failed || code;
      return (MSG.requestResult || "") + "：" + mapped;
    }
    const outcome = (function () {
      if (data.outcome === "success") {
        if (data.match_count === 0) return MSG.sendNoMatches || "";
        return MSG.sendComplete || "";
      }
      if (data.outcome === "partial_errors") return MSG.sendPartial || "";
      if (data.outcome === "graphql_errors") return MSG.sendGraphqlErrors || "";
      if (data.outcome === "http_error") return MSG.sendHttpError || "";
      if (data.outcome === "non_json") return MSG.sendNonJson || "";
      return MSG.sendComplete || "";
    })();
    const httpDisp =
      data.http_status === null || data.http_status === undefined
        ? "—"
        : String(data.http_status);
    const ing = data.ingest || {};
    const isOddsUpdate = ing.request_key === "update_odds_and_results";
    const isResults = ing.request_key === "match_results";
    const isHistoryResults =
      ing.request_key === "match_results" && ing.history_scope === "payload_all";
    const reqVars =
      typeof ctx.getRequestVariables === "function"
        ? ctx.getRequestVariables()
        : null;
    const dateWindow =
      ing.date_window && typeof ing.date_window === "object"
        ? ing.date_window
        : reqVars;
    let text =
      (MSG.requestResult || "") +
      "：" +
      outcome +
      "\n" +
      (MSG.labelHttp || "") +
      "：" +
      httpDisp +
      "\n" +
      (MSG.labelElapsed || "") +
      "：" +
      (data.elapsed_ms || 0) +
      " ms\n\n";
    if (isOddsUpdate) {
      text += (MSG.ingestOddsSection || "") + "：" + _ingestReason(ing, MSG) + "\n";
    } else if (isHistoryResults) {
      const dlOk = ing.download_status === "success";
      text +=
        (MSG.downloadSection || "") +
        "：" +
        (dlOk ? MSG.downloadSuccess || "" : MSG.downloadFailed || "") +
        "\n";
      if (dateWindow) {
        text +=
          (MSG.ingestDateRange || "") +
          "：" +
          String(dateWindow.startDate || "—") +
          " ～ " +
          String(dateWindow.endDate || "—") +
          "\n";
      }
      if (ing.fetch_mode) {
        text += (MSG.ingestFetchMode || "") + "：" + String(ing.fetch_mode) + "\n";
      }
      text += _resultsCompletenessLabel(ing, reqVars, MSG) + "\n";
      text += (MSG.ingestRangeHintMatchResults || "") + "\n";
      const histOk = ing.status === "success" && ing.code === "success";
      const histChanged = (ing.inserted || 0) + (ing.updated || 0) > 0;
      let histLine = _ingestReason(ing, MSG);
      if (histOk && histChanged) histLine = MSG.ingestHistorySuccess || histLine;
      else if (ing.code === "skipped_no_existing" || (histOk && !histChanged)) {
        histLine = MSG.ingestHistoryNoChange || histLine;
      }
      text += (MSG.ingestHistorySection || "") + "：" + histLine + "\n";
    } else if (isResults) {
      text += (MSG.ingestResultsSection || "") + "：" + _ingestReason(ing, MSG) + "\n";
      if (reqVars) {
        text +=
          (MSG.ingestDateRange || "") +
          "：" +
          String(reqVars.startDate || "—") +
          " ～ " +
          String(reqVars.endDate || "—") +
          "\n";
      }
      text += _resultsCompletenessLabel(ing, reqVars, MSG) + "\n";
      text += (MSG.ingestRangeHint || "") + "\n";
    } else {
      text += (MSG.ingestSection || "") + "：" + _ingestReason(ing, MSG) + "\n";
    }
    if (
      (ing.status === "success" && ing.code === "success") ||
      ing.code === "skipped_no_existing"
    ) {
      if (isHistoryResults) {
        const savedCount = (ing.updated || 0) + (ing.inserted || 0);
        text +=
          (MSG.ingestDownloadVsSaved || "") +
          "：" +
          (ing.matches_returned == null ? "—" : String(ing.matches_returned)) +
          " / " +
          savedCount +
          "\n" +
          (MSG.ingestProcessed || "") +
          "：" +
          (ing.processed || 0) +
          "\n" +
          (MSG.ingestHistoryInserted || "") +
          "：" +
          (ing.inserted || 0) +
          "\n" +
          (MSG.ingestHistoryUpdated || "") +
          "：" +
          (ing.updated || 0) +
          "\n" +
          (MSG.ingestDailyDeleted || "") +
          "：" +
          (ing.daily_deleted || 0) +
          "\n" +
          (MSG.ingestHtApplied || "") +
          "：" +
          (ing.ht_applied || 0) +
          "\n" +
          (MSG.ingestFtApplied || "") +
          "：" +
          (ing.ft_applied || 0) +
          "\n" +
          (MSG.ingestShApplied || "") +
          "：" +
          (ing.sh_applied || 0) +
          "\n" +
          (MSG.ingestSkippedUnconfirmed || "") +
          "：" +
          (ing.skipped_unconfirmed || 0) +
          "\n" +
          (MSG.ingestSkippedNoKickoff || "") +
          "：" +
          (ing.skipped_no_kickoff || 0) +
          "\n" +
          (MSG.ingestMatchesReturned || "") +
          "：" +
          (ing.matches_returned == null ? "—" : String(ing.matches_returned)) +
          "\n" +
          (MSG.ingestMatchNumTotal || "") +
          "：" +
          (ing.match_num_total == null ? "—" : String(ing.match_num_total)) +
          "\n";
        if (ing.detail) text += "detail：" + String(ing.detail) + "\n";
        if (ing.status === "success" && savedCount > 0) {
          text +=
            (MSG.syncResultsLabel || "") +
            "：" +
            (ing.last_success_at_hkt || MSG.updatedAtNever || "") +
            "\n";
        } else if (ing.code === "skipped_no_existing" || savedCount === 0) {
          text += (MSG.ingestHistoryNoChange || "") + "\n";
        }
      } else if (isResults) {
        const savedCount = (ing.updated || 0) + (ing.inserted || 0);
        text +=
          (MSG.ingestDownloadVsSaved || "") +
          "：" +
          (ing.matches_returned == null ? "—" : String(ing.matches_returned)) +
          " / " +
          savedCount +
          "\n" +
          (MSG.ingestProcessed || "") +
          "：" +
          (ing.processed || 0) +
          "\n" +
          (MSG.ingestHistoryInserted || "") +
          "：" +
          (ing.inserted || 0) +
          "\n" +
          (MSG.ingestUpdated || "") +
          "：" +
          (ing.updated || 0) +
          "\n" +
          (MSG.ingestHtApplied || "") +
          "：" +
          (ing.ht_applied || 0) +
          "\n" +
          (MSG.ingestFtApplied || "") +
          "：" +
          (ing.ft_applied || 0) +
          "\n" +
          (MSG.ingestShApplied || "") +
          "：" +
          (ing.sh_applied || 0) +
          "\n" +
          (MSG.ingestSkippedMissing || "") +
          "：" +
          (ing.skipped_missing || 0) +
          "\n" +
          (MSG.ingestSkippedStatus || "") +
          "：" +
          (ing.skipped_status || 0) +
          "\n" +
          (MSG.ingestSkippedUnconfirmed || "") +
          "：" +
          (ing.skipped_unconfirmed || 0) +
          "\n" +
          (MSG.ingestSkippedInsufficient || "") +
          "：" +
          (ing.skipped_insufficient || 0) +
          "\n" +
          (MSG.ingestMatchesReturned || "") +
          "：" +
          (ing.matches_returned == null ? "—" : String(ing.matches_returned)) +
          "\n" +
          (MSG.ingestMatchNumTotal || "") +
          "：" +
          (ing.match_num_total == null ? "—" : String(ing.match_num_total)) +
          "\n";
        if (ing.detail) text += "detail：" + String(ing.detail) + "\n";
        if (ing.status === "success") {
          text +=
            (MSG.syncResultsLabel || "") +
            "：" +
            (ing.last_success_at_hkt || MSG.updatedAtNever || "") +
            "\n";
        } else if (ing.code === "skipped_no_existing") {
          text += (MSG.ingestNoExisting || "") + "\n";
        }
      } else {
        text +=
          (MSG.ingestProcessed || "") +
          "：" +
          (ing.processed || 0) +
          "\n" +
          (MSG.ingestInserted || "") +
          "：" +
          (ing.inserted || 0) +
          "\n" +
          (MSG.ingestUpdated || "") +
          "：" +
          (ing.updated || 0) +
          "\n" +
          (MSG.ingestHadOk || "") +
          "：" +
          (ing.had_complete || 0) +
          "\n" +
          (MSG.ingestHadMissing || "") +
          "：" +
          (ing.had_incomplete || 0) +
          "\n";
        if (isOddsUpdate) {
          text +=
            (MSG.ingestSkippedMissing || "") +
            "：" +
            (ing.skipped_missing || 0) +
            "\n";
          text += (MSG.ingestResultsPending || "") + "\n";
          text +=
            (MSG.syncOddsLabel || "") +
            "：" +
            (ing.last_success_at_hkt || MSG.updatedAtNever || "") +
            "\n";
        } else if (ing.status === "success") {
          text +=
            (MSG.updatedAtLabel || "") +
            "：" +
            (ing.last_success_at_hkt || MSG.updatedAtNever || "") +
            "\n";
        }
      }
    }
    text += "\n" + (MSG.labelBody || "") + "：\n" + (data.body || "");
    if (data.truncated) text += "\n\n" + (MSG.truncated || "");
    return text;
  }

  /** Shared Status entry: adapters → status model → one renderer. */
  function formatDownloadStatus(data, MSG, ctx) {
    const shell = global.HppvRecordsShell;
    if (shell && typeof shell.formatStatusModel === "function") {
      const model =
        data && (data.unit === "record" || data.race_date != null)
          ? shell.racingResponseToStatusModel(data, MSG)
          : shell.soccerResponseToStatusModel(data, MSG, ctx);
      return shell.formatStatusModel(model, MSG);
    }
    if (data && (data.unit === "record" || data.race_date != null)) {
      return formatRecordDownloadStatus(data, MSG);
    }
    return formatSoccerGraphqlStatus(data, MSG, ctx);
  }

  function initDownloadShell(opts) {
    const requestType = $(opts.requestTypeId);
    const requestBox = $(opts.requestBoxId);
    const statusBox = $(opts.statusBoxId);
    const btnSend = $(opts.sendBtnId);
    if (!requestType || !requestBox || !statusBox || !btnSend) return null;

    const MSG = opts.messages || {};
    const presetUrl = opts.presetUrl; // (presetId) => url
    const sendUrl = opts.sendUrl;
    const formatCtx = opts.formatContext || {};
    const formatStatus =
      typeof opts.formatStatus === "function"
        ? opts.formatStatus
        : function (data) {
            return formatDownloadStatus(data, MSG, formatCtx);
          };
    const onAfterSuccess =
      typeof opts.onAfterSuccess === "function" ? opts.onAfterSuccess : null;
    const onPresetChange =
      typeof opts.onPresetChange === "function" ? opts.onPresetChange : null;
    const shouldHandlePreset =
      typeof opts.shouldHandlePreset === "function"
        ? opts.shouldHandlePreset
        : function () {
            return true;
          };
    const buildSendBody =
      typeof opts.buildSendBody === "function"
        ? opts.buildSendBody
        : function (presetId, payloadText) {
            return { request_key: presetId, payload: payloadText };
          };

    let loadingPreset = false;
    let sending = false;
    let userEditedRequest = false;
    let autoLoadStarted = false;

    requestBox.addEventListener("input", function () {
      userEditedRequest = true;
    });

    async function loadPreset() {
      if (loadingPreset) return;
      const presetId = requestType.value || opts.defaultPresetId || "";
      if (!shouldHandlePreset(presetId)) return;
      const previousRequest = requestBox.value;
      loadingPreset = true;
      statusBox.value = MSG.loading || "";
      try {
        const resp = await fetch(presetUrl(presetId), {
          method: "GET",
          headers: { Accept: "application/json" },
        });
        let data = null;
        try {
          data = await resp.json();
        } catch (_e) {
          data = null;
        }
        if (!resp.ok || !data || !data.ok) {
          statusBox.value = MSG.loadFailed || MSG.error || "";
          requestBox.value = previousRequest;
          return;
        }
        if (userEditedRequest && requestBox.value !== previousRequest) {
          if (statusBox.value === (MSG.loading || "")) statusBox.value = "";
          return;
        }
        requestBox.value = data.display || "";
        userEditedRequest = false;
        statusBox.value = MSG.loadSuccess || "";
        if (typeof opts.onPresetLoaded === "function") {
          await opts.onPresetLoaded(presetId, data);
        }
      } catch (_e) {
        statusBox.value = MSG.loadFailed || MSG.error || "";
        requestBox.value = previousRequest;
      } finally {
        loadingPreset = false;
      }
    }

    requestType.addEventListener("change", async function () {
      if (typeof onPresetChange === "function") {
        const handled = await onPresetChange(requestType.value);
        if (handled === true) return;
      }
      if (!shouldHandlePreset(requestType.value)) return;
      if (userEditedRequest && requestBox.value.trim() !== "") {
        if (!window.confirm(MSG.overwriteConfirm || "Overwrite Request?")) {
          return;
        }
        userEditedRequest = false;
      }
      autoLoadStarted = false;
      requestBox.value = "";
      await loadPreset();
    });

    btnSend.addEventListener("click", async function () {
      if (sending) return;
      if (typeof opts.canSend === "function" && opts.canSend(requestType.value) === false) {
        return;
      }
      sending = true;
      btnSend.disabled = true;
      statusBox.value = MSG.sending || MSG.fetchingTotal || "";
      const presetId = requestType.value || opts.defaultPresetId || "";
      try {
        const resp = await fetch(sendUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
          },
          body: JSON.stringify(buildSendBody(presetId, requestBox.value)),
        });
        let data = null;
        try {
          data = await resp.json();
        } catch (_e) {
          data = null;
        }
        if (!data) {
          statusBox.value = (MSG.error || "Error") + "（HTTP " + resp.status + "）";
        } else {
          statusBox.value = formatStatus(data);
          if (onAfterSuccess && (resp.ok || data.ok)) {
            onAfterSuccess(data, presetId);
          }
        }
      } catch (_e) {
        statusBox.value = MSG.error || "";
      } finally {
        sending = false;
        btnSend.disabled = false;
      }
    });

    const autoLoad = opts.autoLoad !== false;
    if (autoLoad && !autoLoadStarted && requestBox.value.trim() === "") {
      autoLoadStarted = true;
      if (shouldHandlePreset(requestType.value || opts.defaultPresetId || "")) {
        loadPreset();
      }
    }

    return { loadPreset: loadPreset };
  }

  function initTaskUi(opts) {
    const enabledEl = $(opts.enabledId);
    const intervalEl = $(opts.intervalId);
    const unitEl = $(opts.unitId);
    const lastEl = $(opts.lastId);
    const nextEl = $(opts.nextId);
    const statusEl = $(opts.statusId);
    const msgEl = $(opts.msgId);
    if (!enabledEl || !intervalEl || !unitEl) return;

    const MSG = opts.messages || {};
    let saveTimer = null;
    let applyingRemote = false;
    let pollTimer = null;
    const apiUrl = opts.apiUrl;

    function statusLabel(code) {
      const map = {
        idle: MSG.taskStatusIdle,
        running: MSG.taskStatusRunning,
        success: MSG.taskStatusSuccess,
        failed: MSG.taskStatusFailed,
        skipped: MSG.taskStatusSkipped,
      };
      return map[code] || MSG.taskStatusIdle || code;
    }

    function applyTaskToUi(task) {
      if (!task) return;
      applyingRemote = true;
      enabledEl.checked = !!task.enabled;
      if (task.interval_value) intervalEl.value = String(task.interval_value);
      if (task.interval_unit) unitEl.value = String(task.interval_unit);
      if (lastEl) lastEl.textContent = task.last_run_at_hkt || MSG.taskNever || "—";
      if (nextEl) {
        nextEl.textContent =
          task.enabled && task.next_run_at_hkt
            ? task.next_run_at_hkt
            : MSG.taskNotScheduled || "";
      }
      if (statusEl) {
        const st = task.running ? "running" : task.last_status || "idle";
        statusEl.textContent = statusLabel(st);
      }
      applyingRemote = false;
    }

    async function loadTask() {
      try {
        const resp = await fetch(apiUrl, { headers: { Accept: "application/json" } });
        const data = await resp.json();
        if (!resp.ok || !data || !data.ok) {
          enabledEl.checked = false;
          if (msgEl) msgEl.textContent = MSG.taskSaveFailed || "";
          return;
        }
        applyTaskToUi(data.task);
      } catch (_e) {
        enabledEl.checked = false;
        if (msgEl) msgEl.textContent = MSG.taskSaveFailed || "";
      }
    }

    async function saveTask() {
      if (applyingRemote) return;
      const payload = {
        enabled: !!enabledEl.checked,
        interval_value: Number(intervalEl.value || 1),
        interval_unit: unitEl.value || "mins",
      };
      try {
        const resp = await fetch(apiUrl, {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
          },
          body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (!resp.ok || !data || !data.ok) {
          if (msgEl) msgEl.textContent = MSG.taskSaveFailed || "";
          await loadTask();
          return;
        }
        applyTaskToUi(data.task);
        if (msgEl) msgEl.textContent = MSG.taskSaveOk || "";
      } catch (_e) {
        if (msgEl) msgEl.textContent = MSG.taskSaveFailed || "";
        await loadTask();
      }
    }

    function scheduleSave() {
      if (applyingRemote) return;
      if (saveTimer) window.clearTimeout(saveTimer);
      saveTimer = window.setTimeout(saveTask, 200);
    }

    enabledEl.addEventListener("change", scheduleSave);
    intervalEl.addEventListener("change", scheduleSave);
    unitEl.addEventListener("change", scheduleSave);
    loadTask();
    pollTimer = window.setInterval(loadTask, 5000);
    window.addEventListener("beforeunload", function () {
      if (pollTimer) window.clearInterval(pollTimer);
    });
  }

  global.HppvDownloadShell = {
    initDownloadShell: initDownloadShell,
    initTaskUi: initTaskUi,
    formatDownloadStatus: formatDownloadStatus,
    formatRecordDownloadStatus: formatRecordDownloadStatus,
    formatSoccerGraphqlStatus: formatSoccerGraphqlStatus,
    updateSyncBadge: updateSyncBadge,
  };
})(window);
