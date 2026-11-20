/**
 * Shared records list procedure for Soccer + Racing.
 * Owns: search debounce, pager, load/reload, loading/empty/error UI, list state.
 * Domain pages supply params + row rendering via callbacks.
 * Header/sort helpers live on HppvRecordsHeader (required).
 */
(function (global) {
  "use strict";

  function requireHeader() {
    if (!global.HppvRecordsHeader) {
      throw new Error("HppvRecordsShell requires HppvRecordsHeader");
    }
    return global.HppvRecordsHeader;
  }

  function renderListError(stateEl, message, retryLabel, onRetry) {
    if (!stateEl) return;
    stateEl.hidden = false;
    stateEl.innerHTML = "";
    stateEl.appendChild(document.createTextNode(message + " "));
    if (typeof onRetry === "function") {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "icon-btn";
      btn.textContent = retryLabel || "Retry";
      btn.addEventListener("click", onRetry);
      stateEl.appendChild(btn);
    }
  }

  /**
   * Create a shared list controller.
   *
   * cfg: {
   *   recordsUrl: string | () => string,
   *   els: {
   *     state?, tableWrap?,
   *     searchInput?, searchOp?,
   *     pagerStatus?, pagerFirst?, pagerPrev?, pagerNext?, pagerLast?
   *   },
   *   messages: { loading, empty, error, retry },
   *   pagerMode: "page" | "cursor" | "none",
   *   searchDebounceMs?: number,
   *   initial?: { n?, q?, qOp?, sort? },
   *   getLayout: () => layoutSaved,
   *   getSortableMap?: () => object,
   *   buildParams?: (state) => URLSearchParams,  // domain filters; shell adds n/q/q_op/sort
   *   normalizeResponse?: (data) => {
   *     n, totalPages?, totalCount, q?, qOp?, sort?, allMatches?, date?
   *   },
   *   onSuccess: (data, api) => void,   // render rows / domain side effects
   *   renderLoading?: (api) => void,
   *   renderError?: (api, detail) => void,
   *   renderEmpty?: (api, data) => void,
   *   beforeFetch?: (api) => boolean|void,  // return false to abort
   *   autoBindSearch?: boolean,
   *   autoBindPager?: boolean,
   * }
   */
  function createList(cfg) {
    if (!cfg || !cfg.recordsUrl) throw new Error("recordsUrl required");
    if (typeof cfg.onSuccess !== "function") throw new Error("onSuccess required");
    if (typeof cfg.getLayout !== "function") throw new Error("getLayout required");
    const Header = requireHeader();
    const MSG = cfg.messages || {};
    const els = cfg.els || {};
    const pagerMode = cfg.pagerMode || "page";
    const debounceMs =
      typeof cfg.searchDebounceMs === "number" ? cfg.searchDebounceMs : 300;
    const initial = cfg.initial || {};

    let status = "idle"; // idle|loading|ready|empty|error
    let loading = false;
    let loaded = false;
    let pageN = Number(initial.n) || 1;
    let totalPages = 0;
    let totalCount = 0;
    let q = typeof initial.q === "string" ? initial.q : "";
    let qOp = (initial.qOp || "approx").toString();
    let sortParam = typeof initial.sort === "string" ? initial.sort : "";
    let lastError = "";
    let fetchSeq = 0;
    let searchTimer = null;

    function sortableMap() {
      return typeof cfg.getSortableMap === "function"
        ? cfg.getSortableMap() || null
        : null;
    }

    function sortFromLayout() {
      return Header.sortParamFromLayout(cfg.getLayout(), sortableMap());
    }

    function syncSortFromLayout() {
      sortParam = sortFromLayout();
      return sortParam;
    }

    function recordsUrl() {
      return typeof cfg.recordsUrl === "function"
        ? cfg.recordsUrl()
        : cfg.recordsUrl;
    }

    function api() {
      return {
        status: status,
        loading: loading,
        loaded: loaded,
        pageN: pageN,
        totalPages: totalPages,
        totalCount: totalCount,
        q: q,
        qOp: qOp,
        sortParam: sortParam,
        lastError: lastError,
        els: els,
        messages: MSG,
        syncSortFromLayout: syncSortFromLayout,
        setSortParam: function (s) {
          sortParam = s || "";
        },
        setPageN: function (n) {
          pageN = Number(n) || 1;
        },
        setTotals: function (opts) {
          opts = opts || {};
          if (opts.n != null) pageN = Number(opts.n) || 1;
          if (opts.totalCount != null) totalCount = Number(opts.totalCount) || 0;
          if (opts.totalPages != null) totalPages = Number(opts.totalPages) || 0;
        },
        setQuery: function (nextQ, nextOp) {
          if (typeof nextQ === "string") q = nextQ;
          if (nextOp != null) qOp = String(nextOp || "approx");
        },
        setStatus: function (next) {
          status = next || status;
        },
        load: load,
        reload: reload,
        cycleHeaderSort: cycleHeaderSort,
        updatePager: updatePager,
        showLoading: showLoading,
        showError: showError,
        showEmpty: showEmpty,
        showReady: showReady,
      };
    }

    function updatePager() {
      if (pagerMode === "none") return;
      const st = els.pagerStatus;
      if (typeof cfg.formatPagerStatus === "function" && st) {
        st.textContent = cfg.formatPagerStatus(api());
      } else if (pagerMode === "cursor") {
        const total = totalCount > 0 ? totalCount : 0;
        const n = total > 0 ? pageN : 0;
        if (st) st.textContent = n + " / " + total;
      } else if (st) {
        st.textContent = totalPages
          ? pageN + " / " + totalPages + " · " + totalCount
          : "0 / 0 · 0";
      }
      if (pagerMode === "cursor") {
        const total = totalCount > 0 ? totalCount : 0;
        const disableAll = total <= 1;
        const atFirst = disableAll || pageN <= 1;
        const atLast = disableAll || pageN >= total;
        if (els.pagerFirst) els.pagerFirst.disabled = atFirst;
        if (els.pagerPrev) els.pagerPrev.disabled = atFirst;
        if (els.pagerNext) els.pagerNext.disabled = atLast;
        if (els.pagerLast) els.pagerLast.disabled = atLast;
        return;
      }
      const atFirst = !totalPages || pageN <= 1;
      const atLast = !totalPages || pageN >= totalPages;
      if (els.pagerFirst) els.pagerFirst.disabled = atFirst;
      if (els.pagerPrev) els.pagerPrev.disabled = atFirst;
      if (els.pagerNext) els.pagerNext.disabled = atLast;
      if (els.pagerLast) els.pagerLast.disabled = atLast;
    }

    function showLoading() {
      status = "loading";
      if (typeof cfg.renderLoading === "function") {
        cfg.renderLoading(api());
        return;
      }
      if (els.tableWrap) els.tableWrap.hidden = true;
      if (els.state) {
        els.state.hidden = false;
        els.state.textContent = MSG.loading || "";
      }
    }

    function showError(detail) {
      status = "error";
      lastError = detail || "";
      if (typeof cfg.renderError === "function") {
        cfg.renderError(api(), lastError);
        return;
      }
      if (els.tableWrap) els.tableWrap.hidden = true;
      renderListError(
        els.state,
        (MSG.error || "") + (lastError ? "（" + lastError + "）" : ""),
        MSG.retry,
        function () {
          reload();
        }
      );
    }

    function showEmpty(data) {
      status = "empty";
      loaded = true;
      if (typeof cfg.renderEmpty === "function") {
        cfg.renderEmpty(api(), data);
        return;
      }
      if (els.tableWrap) els.tableWrap.hidden = true;
      if (els.state) {
        els.state.hidden = false;
        els.state.textContent = MSG.empty || "";
      }
    }

    function showReady() {
      status = "ready";
      loaded = true;
      if (els.state) els.state.hidden = true;
      if (els.tableWrap) els.tableWrap.hidden = false;
    }

    function buildRequestParams() {
      const params =
        typeof cfg.buildParams === "function"
          ? cfg.buildParams(api())
          : new URLSearchParams();
      if (!(params instanceof URLSearchParams)) {
        throw new Error("buildParams must return URLSearchParams");
      }
      if (pagerMode !== "none") {
        params.set("n", String(pageN || 1));
      }
      if (q) params.set("q", q);
      if (els.searchOp || qOp) params.set("q_op", qOp || "approx");
      const sort = sortParam || sortFromLayout();
      sortParam = sort;
      if (sort) params.set("sort", sort);
      return params;
    }

    function normalize(data) {
      if (typeof cfg.normalizeResponse === "function") {
        return cfg.normalizeResponse(data) || {};
      }
      return {
        n: data.n,
        totalPages: data.total_pages,
        totalCount: data.total_count,
        q: data.q,
        qOp: data.q_op,
        sort: data.sort,
      };
    }

    function isEmptyPayload(data, norm) {
      if (Array.isArray(data.rows)) return data.rows.length === 0;
      if (Array.isArray(data.matches)) return data.matches.length === 0;
      if (norm && typeof norm.totalCount === "number") return norm.totalCount <= 0;
      return false;
    }

    async function load(opts) {
      opts = opts || {};
      if (opts.n != null) pageN = Number(opts.n) || 1;
      if (opts.q != null) q = String(opts.q);
      if (opts.qOp != null) qOp = String(opts.qOp || "approx");
      if (opts.resetPage) pageN = 1;

      if (typeof cfg.beforeFetch === "function") {
        if (cfg.beforeFetch(api()) === false) return false;
      }

      const seq = ++fetchSeq;
      loading = true;
      showLoading();
      updatePager();

      try {
        const params = buildRequestParams();
        const resp = await fetch(recordsUrl() + "?" + params.toString(), {
          headers: { Accept: "application/json" },
        });
        let data = null;
        try {
          data = await resp.json();
        } catch (_e) {
          data = null;
        }
        if (seq !== fetchSeq) return false;

        if (!resp.ok || !data || !data.ok) {
          const detail =
            data && data.error ? String(data.error) : "http_" + resp.status;
          status = "error";
          lastError = detail;
          if (typeof cfg.renderError === "function") {
            cfg.renderError(api(), lastError, data);
          } else {
            showError(detail);
          }
          updatePager();
          return false;
        }

        const norm = normalize(data);
        if (norm.n != null) pageN = Number(norm.n) || pageN;
        if (norm.totalPages != null) totalPages = Number(norm.totalPages) || 0;
        if (norm.totalCount != null) totalCount = Number(norm.totalCount) || 0;
        if (typeof norm.q === "string") q = norm.q;
        if (norm.qOp != null) qOp = String(norm.qOp || qOp);
        if (typeof norm.sort === "string") {
          sortParam = norm.sort;
          Header.applySortParamToLayout(
            cfg.getLayout(),
            sortParam,
            sortableMap()
          );
        }

        if (isEmptyPayload(data, norm)) {
          showEmpty(data);
          updatePager();
          cfg.onSuccess(data, api());
          return true;
        }

        showReady();
        updatePager();
        cfg.onSuccess(data, api());
        return true;
      } catch (_e) {
        if (seq !== fetchSeq) return false;
        showError("network");
        updatePager();
        return false;
      } finally {
        if (seq === fetchSeq) loading = false;
      }
    }

    function reload() {
      return load({ n: pageN });
    }

    function cycleHeaderSort(key) {
      const layout = cfg.getLayout();
      const draft =
        typeof cfg.getDraftLayout === "function"
          ? cfg.getDraftLayout()
          : cfg.draftLayout || null;
      if (
        !Header.cycleLayoutSort(layout, key, {
          sortableMap: sortableMap(),
          draftLayout: draft,
        })
      ) {
        return false;
      }
      syncSortFromLayout();
      return load({ resetPage: pagerMode === "page", n: pagerMode === "cursor" ? 1 : pageN });
    }

    function bindSearch() {
      if (!els.searchInput) return;
      const commit = function () {
        q = (els.searchInput.value || "").trim();
        if (els.searchOp) qOp = (els.searchOp.value || "approx").toString();
        load({ resetPage: true, n: 1 });
      };
      els.searchInput.addEventListener("input", function () {
        if (searchTimer) global.clearTimeout(searchTimer);
        searchTimer = global.setTimeout(commit, debounceMs);
      });
      els.searchInput.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          if (searchTimer) global.clearTimeout(searchTimer);
          commit();
        }
      });
      els.searchInput.addEventListener("search", function () {
        if (searchTimer) global.clearTimeout(searchTimer);
        commit();
      });
      if (els.searchOp) {
        els.searchOp.addEventListener("change", function () {
          qOp = (els.searchOp.value || "approx").toString();
          load({ resetPage: true, n: 1 });
        });
      }
    }

    function bindPager() {
      if (pagerMode === "none") return;
      if (pagerMode === "cursor") {
        if (els.pagerFirst) {
          els.pagerFirst.addEventListener("click", function () {
            if (!els.pagerFirst.disabled) {
              pageN = 1;
              updatePager();
              if (typeof cfg.onCursorMove === "function") cfg.onCursorMove(api());
            }
          });
        }
        if (els.pagerPrev) {
          els.pagerPrev.addEventListener("click", function () {
            if (!els.pagerPrev.disabled) {
              pageN = Math.max(1, pageN - 1);
              updatePager();
              if (typeof cfg.onCursorMove === "function") cfg.onCursorMove(api());
            }
          });
        }
        if (els.pagerNext) {
          els.pagerNext.addEventListener("click", function () {
            if (!els.pagerNext.disabled) {
              pageN = Math.min(totalCount, pageN + 1);
              updatePager();
              if (typeof cfg.onCursorMove === "function") cfg.onCursorMove(api());
            }
          });
        }
        if (els.pagerLast) {
          els.pagerLast.addEventListener("click", function () {
            if (!els.pagerLast.disabled) {
              pageN = totalCount || 1;
              updatePager();
              if (typeof cfg.onCursorMove === "function") cfg.onCursorMove(api());
            }
          });
        }
        return;
      }
      if (els.pagerFirst) {
        els.pagerFirst.addEventListener("click", function () {
          if (!els.pagerFirst.disabled) load({ n: 1 });
        });
      }
      if (els.pagerPrev) {
        els.pagerPrev.addEventListener("click", function () {
          if (!els.pagerPrev.disabled) load({ n: Math.max(1, pageN - 1) });
        });
      }
      if (els.pagerNext) {
        els.pagerNext.addEventListener("click", function () {
          if (!els.pagerNext.disabled) load({ n: pageN + 1 });
        });
      }
      if (els.pagerLast) {
        els.pagerLast.addEventListener("click", function () {
          if (!els.pagerLast.disabled) load({ n: totalPages || 1 });
        });
      }
    }

    if (cfg.autoBindSearch !== false) bindSearch();
    if (cfg.autoBindPager !== false) bindPager();
    updatePager();

    return api();
  }

  global.HppvRecordsShell = {
    createList: createList,
    renderListError: renderListError,
    // Prefer Header module; re-export for one import surface.
    sortParamFromLayout: function (layout, map) {
      return requireHeader().sortParamFromLayout(layout, map);
    },
    cycleLayoutSort: function (layout, key, opts) {
      return requireHeader().cycleLayoutSort(layout, key, opts);
    },
    applySortParamToLayout: function (layout, sort, map) {
      return requireHeader().applySortParamToLayout(layout, sort, map);
    },
    bindHeaderClicks: function (opts) {
      return requireHeader().bindHeaderClicks(opts);
    },
    headerLabel: function (col, labels) {
      return requireHeader().headerLabel(col, labels);
    },
  };
})(window);
