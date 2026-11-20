/**
 * Shared list header / sort helpers (soccer + racing).
 * Only header sort state + click binding — not list fetch/search/pager.
 */
(function (global) {
  "use strict";

  function sortArrowSuffix(sort) {
    if (sort === "asc") return " ↑";
    if (sort === "desc") return " ↓";
    return "";
  }

  function nextSortDir(current) {
    if (current === "asc") return "desc";
    if (current === "desc") return "";
    return "asc";
  }

  /**
   * Build records `sort` query from layout column.sort fields.
   * Order follows layout.columns order (same as soccer daily).
   */
  function sortParamFromLayout(layoutSaved, sortableMap) {
    const parts = [];
    const cols = (layoutSaved && layoutSaved.columns) || [];
    cols.forEach(function (c) {
      if (!c || !c.key) return;
      if (sortableMap && sortableMap[c.key] === false) return;
      if (c.sort === "asc" || c.sort === "desc") {
        parts.push(c.key + ":" + c.sort);
      }
    });
    return parts.join(",");
  }

  /**
   * Cycle one column sort in place: "" → asc → desc → "".
   * Does not reorder columns (list column order stays put).
   * Optionally mirrors the same key on draftLayout columns.
   */
  function cycleLayoutSort(layoutSaved, key, opts) {
    opts = opts || {};
    const sortableMap = opts.sortableMap || null;
    const draftLayout = opts.draftLayout || null;
    if (!layoutSaved || !Array.isArray(layoutSaved.columns)) {
      throw new Error("layoutSaved.columns required");
    }
    if (sortableMap && sortableMap[key] === false) return false;
    const col = layoutSaved.columns.find(function (c) {
      return c && c.key === key;
    });
    if (!col) return false;
    col.sort = nextSortDir(col.sort || "");
    if (draftLayout && Array.isArray(draftLayout.columns)) {
      const draftCol = draftLayout.columns.find(function (c) {
        return c && c.key === key;
      });
      if (draftCol) draftCol.sort = col.sort;
    }
    return true;
  }

  /**
   * Apply a `key:dir,...` sort string onto layout column.sort (header arrows).
   * Unknown keys ignored; columns not in param cleared to "".
   */
  function applySortParamToLayout(layoutSaved, sortParam, sortableMap) {
    if (!layoutSaved || !Array.isArray(layoutSaved.columns)) return;
    const wanted = {};
    String(sortParam || "")
      .split(",")
      .map(function (p) {
        return p.trim();
      })
      .filter(Boolean)
      .forEach(function (p) {
        const i = p.indexOf(":");
        if (i <= 0) return;
        const k = p.slice(0, i);
        const d = p.slice(i + 1);
        if (d === "asc" || d === "desc") wanted[k] = d;
      });
    layoutSaved.columns.forEach(function (c) {
      if (!c || !c.key) return;
      if (sortableMap && sortableMap[c.key] === false) {
        c.sort = "";
        return;
      }
      c.sort = wanted[c.key] || "";
    });
  }

  /**
   * Bind click on header cells. opts:
   *   root: Element
   *   selector: default 'th[data-col], th[data-key]'
   *   keyAttr: tried in order — data-col, data-key
   *   onCycle(key): required
   */
  function bindHeaderClicks(opts) {
    opts = opts || {};
    const root = opts.root;
    if (!root) return 0;
    const selector = opts.selector || "th[data-col], th[data-key]";
    const onCycle = opts.onCycle;
    if (typeof onCycle !== "function") {
      throw new Error("onCycle required");
    }
    let n = 0;
    root.querySelectorAll(selector).forEach(function (th) {
      const key =
        th.getAttribute("data-col") ||
        th.getAttribute("data-key") ||
        "";
      if (!key) return;
      th.style.cursor = "pointer";
      th.addEventListener("click", function () {
        onCycle(key);
      });
      n += 1;
    });
    return n;
  }

  function headerLabel(col, labels) {
    const key = col && col.key;
    const map = labels || {};
    const base = (key && map[key]) || key || "";
    return base + sortArrowSuffix(col && col.sort);
  }

  global.HppvRecordsHeader = {
    sortArrowSuffix: sortArrowSuffix,
    nextSortDir: nextSortDir,
    sortParamFromLayout: sortParamFromLayout,
    cycleLayoutSort: cycleLayoutSort,
    applySortParamToLayout: applySortParamToLayout,
    bindHeaderClicks: bindHeaderClicks,
    headerLabel: headerLabel,
  };
})(window);
