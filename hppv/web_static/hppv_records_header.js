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

  /**
   * Set one column width on layout (+ optional draft). Returns clamped width or false.
   */
  function setLayoutColumnWidth(layoutSaved, key, width, opts) {
    opts = opts || {};
    const widthMin = opts.widthMin != null ? Number(opts.widthMin) : 60;
    const widthMax = opts.widthMax != null ? Number(opts.widthMax) : 600;
    const draftLayout = opts.draftLayout || null;
    if (!layoutSaved || !Array.isArray(layoutSaved.columns)) {
      throw new Error("layoutSaved.columns required");
    }
    let w = Math.round(Number(width));
    if (!Number.isFinite(w)) return false;
    w = Math.max(widthMin, Math.min(widthMax, w));
    const col = layoutSaved.columns.find(function (c) {
      return c && c.key === key;
    });
    if (!col) return false;
    col.width = w;
    if (draftLayout && Array.isArray(draftLayout.columns)) {
      const draftCol = draftLayout.columns.find(function (c) {
        return c && c.key === key;
      });
      if (draftCol) draftCol.width = w;
    }
    return w;
  }

  function applyWidthToColumnCells(th, width) {
    if (!th || !th.parentNode) return;
    const table = th.closest ? th.closest("table") : null;
    if (!table) return;
    const idx = Array.prototype.indexOf.call(th.parentNode.children, th);
    if (idx < 0) return;
    const px = width + "px";
    th.style.width = px;
    th.style.minWidth = px;
    table.querySelectorAll("tbody tr").forEach(function (tr) {
      const td = tr.children[idx];
      if (!td) return;
      td.style.width = px;
      td.style.minWidth = px;
    });
  }

  /**
   * Bind drag-resize on header cells. opts:
   *   root, selector, widthMin, widthMax
   *   onCommit(key, width): required (after mouseup)
   *   onDrag(key, width, th): optional live callback
   */
  function bindHeaderResize(opts) {
    opts = opts || {};
    const root = opts.root;
    if (!root) return 0;
    const selector = opts.selector || "th[data-col], th[data-key]";
    const onCommit = opts.onCommit;
    if (typeof onCommit !== "function") {
      throw new Error("onCommit required");
    }
    const widthMin = opts.widthMin != null ? Number(opts.widthMin) : 60;
    const widthMax = opts.widthMax != null ? Number(opts.widthMax) : 600;
    let n = 0;
    root.querySelectorAll(selector).forEach(function (th) {
      const key =
        th.getAttribute("data-col") ||
        th.getAttribute("data-key") ||
        "";
      if (!key) return;
      if (window.getComputedStyle(th).position === "static") {
        th.style.position = "relative";
      }
      let handle = th.querySelector(".col-resize-handle");
      if (!handle) {
        handle = document.createElement("span");
        handle.className = "col-resize-handle";
        handle.setAttribute("aria-hidden", "true");
        th.appendChild(handle);
      }
      handle.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
      });
      handle.addEventListener("mousedown", function (ev) {
        if (ev.button !== 0) return;
        ev.preventDefault();
        ev.stopPropagation();
        const startX = ev.clientX;
        const startW = th.getBoundingClientRect().width;
        let latest = Math.round(startW);
        let moved = false;
        document.body.classList.add("is-col-resizing");
        function onMove(e) {
          const next = Math.round(startW + (e.clientX - startX));
          const clamped = Math.max(widthMin, Math.min(widthMax, next));
          if (clamped !== latest || e.clientX !== startX) moved = true;
          latest = clamped;
          applyWidthToColumnCells(th, latest);
          if (typeof opts.onDrag === "function") {
            opts.onDrag(key, latest, th);
          }
        }
        function onUp(e) {
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", onUp);
          document.body.classList.remove("is-col-resizing");
          // Swallow the click that follows a drag so sort is not toggled.
          if (moved) {
            const swallow = function (ce) {
              ce.preventDefault();
              ce.stopPropagation();
              document.removeEventListener("click", swallow, true);
            };
            document.addEventListener("click", swallow, true);
            setTimeout(function () {
              document.removeEventListener("click", swallow, true);
            }, 0);
          }
          onCommit(key, latest);
        }
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
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
    setLayoutColumnWidth: setLayoutColumnWidth,
    applyWidthToColumnCells: applyWidthToColumnCells,
    bindHeaderClicks: bindHeaderClicks,
    bindHeaderResize: bindHeaderResize,
    headerLabel: headerLabel,
  };
})(window);
