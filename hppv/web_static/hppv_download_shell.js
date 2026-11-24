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
   * Shared status for unit=record (racing) — aligned with soccer download Status:
   * 請求結果 / HTTP / 耗時 / 入庫結果 / 筆數；錯誤依 sendErrors 對碼，不一律「連線失敗」。
   */
  function formatRecordDownloadStatus(data, MSG) {
    MSG = MSG || {};
    const reqPrefix = MSG.requestResult || "";
    if (!data) {
      return reqPrefix
        ? reqPrefix + "：" + (MSG.failed || MSG.error || "")
        : MSG.error || "";
    }
    if (data.ok === false) {
      const code = data.error || "read_failed";
      const mapped =
        (MSG.sendErrors && MSG.sendErrors[code]) ||
        MSG.failed ||
        code;
      let line = reqPrefix ? reqPrefix + "：" + mapped : mapped;
      if (data.detail) line += " — " + data.detail;
      return line;
    }
    const jobRunning =
      data.job_status === "running" ||
      data.job_status === "queued" ||
      data.outcome === "running";
    const outcome = jobRunning
      ? MSG.jobRunning || MSG.fetchingTotal || MSG.sending || "下載中…"
      : data.outcome === "success"
        ? MSG.sendComplete || ""
        : MSG.failed || MSG.error || "";
    const httpDisp =
      data.http_status === null || data.http_status === undefined
        ? "—"
        : String(data.http_status);
    const ing = data.ingest || {};
    const detail = data.detail || ing.detail || {};
    const processed =
      (data.processed != null ? data.processed : ing.processed) || 0;
    const updated =
      (data.updated != null ? data.updated : ing.updated) || 0;
    const skipped =
      (data.skipped != null ? data.skipped : ing.skipped) || 0;
    const failed =
      (data.failed != null ? data.failed : ing.failed) || 0;
    const total = data.total != null ? data.total : ing.total;
    const ingestCode = ing.code || data.outcome || "";
    let ingestReason = MSG.ingestOk || MSG.sendComplete || "";
    if (jobRunning) {
      ingestReason = MSG.jobRunning || MSG.fetchingTotal || "進行中";
    } else if (ingestCode === "partial_failure") {
      ingestReason = MSG.ingestPartial || ingestReason;
    } else if (ing.status && ing.status !== "success") {
      ingestReason = MSG.failed || ingestReason;
    }
    let text =
      (reqPrefix ? reqPrefix + "：" + outcome : outcome) +
      "\n" +
      (MSG.labelHttp || "HTTP") +
      "：" +
      httpDisp +
      "\n" +
      (MSG.labelElapsed || "") +
      "：" +
      (data.elapsed_ms || 0) +
      " ms\n\n";
    text +=
      (MSG.ingestSection || "") +
      "：" +
      ingestReason +
      "\n";
    if (data.message) {
      text += (MSG.progressMessage || "進度") + "：" + data.message + "\n";
    }
    const dateWindow =
      (ing.date_window && typeof ing.date_window === "object"
        ? ing.date_window
        : null) ||
      (detail.variables &&
      (detail.variables.startDate != null || detail.variables.endDate != null)
        ? detail.variables
        : null);
    if (dateWindow) {
      text +=
        (MSG.ingestDateRange || "") +
        "：" +
        String(dateWindow.startDate || "—") +
        " ～ " +
        String(dateWindow.endDate || "—") +
        "\n";
      if (dateWindow.venueCodes) {
        const vc = Array.isArray(dateWindow.venueCodes)
          ? dateWindow.venueCodes.join(",")
          : String(dateWindow.venueCodes);
        text += (MSG.venueCodesLabel || "venueCodes") + "：" + vc + "\n";
      }
    }
    if (detail.meetings_scanned != null) {
      text +=
        (MSG.meetingsScanned || "meetings_scanned") +
        "：" +
        detail.meetings_scanned +
        "\n";
    }
    if (detail.meetings_with_results != null) {
      text +=
        (MSG.meetingsWithResults || "meetings_with_results") +
        "：" +
        detail.meetings_with_results +
        "\n";
    }
    text +=
      (MSG.ingestProcessed || "") +
      "：" +
      processed +
      (total == null ? "" : " / " + total) +
      "\n" +
      (MSG.updated || "") +
      "：" +
      updated +
      "\n" +
      (MSG.skipped || "") +
      "：" +
      skipped +
      "\n" +
      (MSG.failed || "") +
      "：" +
      failed +
      "\n";
    if (data.race_date) text += "race_date：" + data.race_date + "\n";
    if (data.venue_code) text += "venue：" + data.venue_code + "\n";
    if (detail.operation) text += "operation：" + detail.operation + "\n";
    if (detail.odds_path) text += "odds_path：" + detail.odds_path + "\n";
    if (data.request_key || ing.request_key) {
      text +=
        "request_key：" + (data.request_key || ing.request_key) + "\n";
    }
    return text.replace(/\n$/, "");
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
    const overwriteEl = opts.overwriteId ? $(opts.overwriteId) : null;
    const buildSendBody =
      typeof opts.buildSendBody === "function"
        ? opts.buildSendBody
        : function (presetId, payloadText) {
            return {
              request_key: presetId,
              payload: payloadText,
              overwrite: !!(overwriteEl && overwriteEl.checked),
            };
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

    const jobStatusUrl =
      typeof opts.jobStatusUrl === "function"
        ? opts.jobStatusUrl
        : typeof opts.jobStatusUrl === "string"
          ? function (jobId) {
              return opts.jobStatusUrl + encodeURIComponent(jobId);
            }
          : null;
    const pollMs =
      typeof opts.jobPollMs === "number" && opts.jobPollMs >= 400
        ? opts.jobPollMs
        : 1000;

    async function pollRacingJob(jobId, presetId) {
      if (!jobStatusUrl) return null;
      for (;;) {
        await new Promise(function (resolve) {
          setTimeout(resolve, pollMs);
        });
        let snap = null;
        try {
          const r = await fetch(jobStatusUrl(jobId), {
            headers: { Accept: "application/json" },
          });
          snap = await r.json();
          if (!r.ok && (!snap || snap.error === "job_not_found")) {
            return {
              ok: false,
              error: (snap && snap.error) || "job_not_found",
            };
          }
        } catch (_e) {
          // Keep polling on transient network blips while job may still run.
          continue;
        }
        if (!snap) continue;
        statusBox.value = formatStatus(snap);
        const st = snap.job_status;
        if (st === "success" || st === "failed") {
          return snap;
        }
      }
    }

    btnSend.addEventListener("click", async function () {
      if (sending) return;
      if (typeof opts.canSend === "function" && opts.canSend(requestType.value) === false) {
        return;
      }
      const rawPayload = requestBox.value;
      // Align with soccer: validate Request JSON before send.
      try {
        JSON.parse(rawPayload);
      } catch (_e) {
        const invalid =
          (MSG.sendErrors && MSG.sendErrors.invalid_json) ||
          MSG.sendInvalidJson ||
          "invalid_json";
        statusBox.value = MSG.requestResult
          ? MSG.requestResult + "：" + invalid
          : invalid;
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
          body: JSON.stringify(buildSendBody(presetId, rawPayload)),
        });
        let data = null;
        try {
          data = await resp.json();
        } catch (_e) {
          data = null;
        }
        if (!resp.ok || !data || data.ok === false) {
          // 202 accepted job still has ok:true; treat as success path below.
          const code =
            data && data.error
              ? data.error
              : !resp.ok
                ? "read_failed"
                : "read_failed";
          if (MSG.requestResult && MSG.sendErrors) {
            statusBox.value =
              MSG.requestResult +
              "：" +
              (MSG.sendErrors[code] || MSG.failed || code) +
              (data && data.detail ? " — " + data.detail : "");
          } else if (!data) {
            statusBox.value =
              (MSG.error || "Error") + "（HTTP " + resp.status + "）";
          } else {
            statusBox.value = formatStatus(data);
          }
          return;
        }
        statusBox.value = formatStatus(data);
        if (
          data.job_id &&
          (data.accepted ||
            data.job_status === "running" ||
            data.job_status === "queued") &&
          jobStatusUrl
        ) {
          data = await pollRacingJob(data.job_id, presetId);
          if (data) statusBox.value = formatStatus(data);
        }
        if (data && onAfterSuccess && (data.ok || data.job_status === "success")) {
          onAfterSuccess(data, presetId);
        }
      } catch (_e) {
        const conn =
          (MSG.sendErrors && MSG.sendErrors.connection_failed) ||
          MSG.sendConnFail ||
          MSG.error ||
          "";
        statusBox.value = MSG.requestResult
          ? MSG.requestResult + "：" + conn
          : conn;
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
    const overwriteEl = opts.overwriteId ? $(opts.overwriteId) : null;
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
      if (overwriteEl) overwriteEl.checked = !!task.overwrite;
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
          if (overwriteEl) overwriteEl.checked = false;
          if (msgEl) msgEl.textContent = MSG.taskSaveFailed || "";
          return;
        }
        applyTaskToUi(data.task);
      } catch (_e) {
        enabledEl.checked = false;
        if (overwriteEl) overwriteEl.checked = false;
        if (msgEl) msgEl.textContent = MSG.taskSaveFailed || "";
      }
    }

    async function saveTask() {
      if (applyingRemote) return;
      const payload = {
        enabled: !!enabledEl.checked,
        overwrite: !!(overwriteEl && overwriteEl.checked),
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
    if (overwriteEl) overwriteEl.addEventListener("change", scheduleSave);
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
