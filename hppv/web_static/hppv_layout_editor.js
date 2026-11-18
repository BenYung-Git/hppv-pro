/**
 * Shared layout-settings editor core (HPPV).
 * Column grouping follows soccer-daily behavior: visible first, hidden last
 * sorted by localized column label for the active UI language.
 * Does not auto-persist; callers save explicitly.
 */
(function (global) {
  "use strict";

  function requireValue(cfg, path, value) {
    if (value === undefined || value === null || value === "") {
      throw new Error("HppvLayoutEditor missing required config: " + path);
    }
    return value;
  }

  function requireObject(cfg, path, value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("HppvLayoutEditor missing required config object: " + path);
    }
    return value;
  }

  function requireFn(cfg, path, value) {
    if (typeof value !== "function") {
      throw new Error("HppvLayoutEditor missing required function: " + path);
    }
    return value;
  }

  function requireEl(id, path) {
    const el = document.getElementById(id);
    if (!el) {
      throw new Error("HppvLayoutEditor missing DOM #" + id + " (" + path + ")");
    }
    return el;
  }

  function deepClone(obj) {
    return JSON.parse(JSON.stringify(obj));
  }

  function HppvLayoutEditor(raw) {
    const cfg = requireObject(raw, "config", raw);
    this.cfg = cfg;
    this.ids = requireObject(cfg, "ids", cfg.ids);
    this.messages = requireObject(cfg, "messages", cfg.messages);
    this.colLabels = requireObject(cfg, "colLabels", cfg.colLabels);
    this.colDescs = requireObject(cfg, "colDescs", cfg.colDescs);
    this.sortableMap = requireObject(cfg, "sortableMap", cfg.sortableMap);
    this.zebraColors = cfg.zebraColors;
    if (!Array.isArray(this.zebraColors) || !this.zebraColors.length) {
      throw new Error("HppvLayoutEditor missing required config: zebraColors");
    }
    this.widthMin = requireValue(cfg, "widthMin", cfg.widthMin);
    this.widthMax = requireValue(cfg, "widthMax", cfg.widthMax);
    this.locale = requireValue(cfg, "locale", cfg.locale);
    this.layoutSaved = deepClone(requireObject(cfg, "initialSettings", cfg.initialSettings));
    if (!Array.isArray(this.layoutSaved.columns)) {
      throw new Error("HppvLayoutEditor initialSettings.columns must be an array");
    }
    this.layoutDraft = deepClone(this.layoutSaved);
    this.saveFn = requireFn(cfg, "save", cfg.save);
    this.onAfterSave = cfg.onAfterSave;
    this.onModeChange = cfg.onModeChange;
    this.applyZebraCss = requireFn(cfg, "applyZebraCss", cfg.applyZebraCss);
    this.autoBind = requireObject(cfg, "autoBind", cfg.autoBind);
    for (const k of ["open", "cancel", "save", "zebra"]) {
      if (typeof this.autoBind[k] !== "boolean") {
        throw new Error("HppvLayoutEditor autoBind." + k + " must be boolean");
      }
    }
    this.modeOpen = false;

    this.els = {
      mode: requireEl(this.ids.mode, "ids.mode"),
      openBtn: requireEl(this.ids.openBtn, "ids.openBtn"),
      cancelBtn: requireEl(this.ids.cancelBtn, "ids.cancelBtn"),
      saveBtn: requireEl(this.ids.saveBtn, "ids.saveBtn"),
      colsBody: requireEl(this.ids.colsBody, "ids.colsBody"),
      zebraColor: requireEl(this.ids.zebraColor, "ids.zebraColor"),
      zebraEnable: requireEl(this.ids.zebraEnable, "ids.zebraEnable"),
      status: requireEl(this.ids.status, "ids.status"),
    };
    if (this.ids.toast) {
      this.els.toast = requireEl(this.ids.toast, "ids.toast");
    }
    if (this.ids.listMode) {
      this.els.listMode = requireEl(this.ids.listMode, "ids.listMode");
    }

    for (const col of this.layoutSaved.columns) {
      if (!col || !col.key) {
        throw new Error("HppvLayoutEditor initialSettings.columns entry missing key");
      }
      if (!(col.key in this.sortableMap)) {
        throw new Error("HppvLayoutEditor sortableMap missing key: " + col.key);
      }
      if (!(col.key in this.colLabels)) {
        throw new Error("HppvLayoutEditor colLabels missing key: " + col.key);
      }
      if (!(col.key in this.colDescs)) {
        throw new Error("HppvLayoutEditor colDescs missing key: " + col.key);
      }
    }

    this._bind();
  }

  HppvLayoutEditor.prototype.isSortable = function (key) {
    return this.sortableMap[key] === true;
  };

  HppvLayoutEditor.prototype.orderedDraftColumns = function () {
    const visible = this.layoutDraft.columns.filter((c) => c.visible);
    const hidden = this.layoutDraft.columns
      .filter((c) => !c.visible)
      .slice()
      .sort((a, b) =>
        String(this.colLabels[a.key] || a.key).localeCompare(
          String(this.colLabels[b.key] || b.key),
          this.locale
        )
      );
    return visible.concat(hidden);
  };

  HppvLayoutEditor.prototype.setDraftOrder = function (orderedKeys) {
    const map = {};
    for (const c of this.layoutDraft.columns) map[c.key] = c;
    const next = [];
    for (const k of orderedKeys) {
      if (!map[k]) {
        throw new Error("HppvLayoutEditor unknown column key in order: " + k);
      }
      next.push(map[k]);
    }
    if (next.length !== this.layoutDraft.columns.length) {
      throw new Error("HppvLayoutEditor setDraftOrder length mismatch");
    }
    this.layoutDraft.columns = next;
  };

  HppvLayoutEditor.prototype.syncZebraControls = function () {
    this.els.zebraColor.value = this.layoutDraft.zebra_color_id;
    this.els.zebraEnable.checked = !!this.layoutDraft.zebra_enabled;
  };

  HppvLayoutEditor.prototype.showToast = function (text) {
    const el = this.els.toast;
    if (!el) return;
    el.textContent = text || "";
    el.classList.toggle("show", !!text);
    if (this._toastTimer) clearTimeout(this._toastTimer);
    if (text) {
      this._toastTimer = setTimeout(() => {
        el.textContent = "";
        el.classList.remove("show");
      }, 4000);
    }
  };

  HppvLayoutEditor.prototype.setMode = function (on) {
    this.modeOpen = !!on;
    if (this.modeOpen) {
      this.els.mode.removeAttribute("hidden");
      if (this.els.listMode) this.els.listMode.setAttribute("hidden", "hidden");
      this.layoutDraft = deepClone(this.layoutSaved);
      this.syncZebraControls();
      this.els.status.textContent = "";
      this.render();
    } else {
      this.els.mode.setAttribute("hidden", "hidden");
      if (this.els.listMode) this.els.listMode.removeAttribute("hidden");
    }
    this.els.openBtn.setAttribute("aria-expanded", this.modeOpen ? "true" : "false");
    if (typeof this.onModeChange === "function") {
      this.onModeChange(this.modeOpen);
    }
  };

  HppvLayoutEditor.prototype.render = function () {
    const ordered = this.orderedDraftColumns();
    this.setDraftOrder(ordered.map((c) => c.key));
    this.applyZebraCss(this.layoutDraft);
    const tbody = this.els.colsBody;
    tbody.innerHTML = "";
    const self = this;
    this.layoutDraft.columns.forEach((col, idx) => {
      const sortable = self.isSortable(col.key);
      const tr = document.createElement("tr");
      if (self.layoutDraft.zebra_enabled && idx % 2 === 1) tr.classList.add("layout-zebra");
      tr.draggable = true;
      tr.dataset.key = col.key;
      tr.innerHTML =
        '<td class="drag-handle">☰</td>' +
        '<td><input type="checkbox" class="lay-vis"' + (col.visible ? " checked" : "") + "></td>" +
        "<td></td><td></td>" +
        '<td><input type="number" class="layout-width-input lay-width" min="' +
        self.widthMin +
        '" max="' +
        self.widthMax +
        '" step="1"></td>' +
        "<td>" +
        (sortable
          ? "<select class='lay-sort'>" +
            '<option value="">' +
            self.messages.sortEmpty +
            "</option>" +
            '<option value="asc">' +
            self.messages.sortAsc +
            "</option>" +
            '<option value="desc">' +
            self.messages.sortDesc +
            "</option>" +
            "</select>"
          : '<span class="muted" title="' +
            self.messages.sortUnsupported +
            '">—</span>') +
        "</td>" +
        "<td>" +
        '<button type="button" class="icon-btn lay-up" title="' +
        self.messages.moveUp +
        '">↑</button> ' +
        '<button type="button" class="icon-btn lay-down" title="' +
        self.messages.moveDown +
        '">↓</button>' +
        "</td>";
      tr.children[2].textContent = self.colLabels[col.key];
      tr.children[3].textContent = self.colDescs[col.key];
      const widthInput = tr.querySelector(".lay-width");
      widthInput.value = String(col.width);
      const sortSel = tr.querySelector(".lay-sort");
      if (sortSel) sortSel.value = col.sort || "";
      const upBtn = tr.querySelector(".lay-up");
      const downBtn = tr.querySelector(".lay-down");
      upBtn.disabled = idx === 0;
      downBtn.disabled = idx === self.layoutDraft.columns.length - 1;

      tr.querySelector(".lay-vis").addEventListener("change", (e) => {
        const on = e.target.checked;
        col.visible = on;
        if (on) {
          const keys = self.layoutDraft.columns.map((c) => c.key).filter((k) => k !== col.key);
          const lastVis = self.layoutDraft.columns.filter((c) => c.visible && c.key !== col.key).pop();
          const insertAt = lastVis ? keys.indexOf(lastVis.key) + 1 : 0;
          keys.splice(insertAt, 0, col.key);
          self.setDraftOrder(keys);
        }
        self.render();
      });
      widthInput.addEventListener("change", () => {
        const n = Number(widthInput.value);
        if (!Number.isInteger(n) || n < self.widthMin || n > self.widthMax) {
          widthInput.classList.add("invalid");
          self.els.status.textContent = self.messages.widthInvalid;
          return;
        }
        widthInput.classList.remove("invalid");
        col.width = n;
        self.els.status.textContent = "";
        self.render();
      });
      if (sortSel) {
        sortSel.addEventListener("change", () => {
          col.sort = sortSel.value;
          self.render();
        });
      }
      upBtn.addEventListener("click", () => {
        if (idx === 0) return;
        const arr = self.layoutDraft.columns;
        const tmp = arr[idx - 1];
        arr[idx - 1] = arr[idx];
        arr[idx] = tmp;
        self.render();
      });
      downBtn.addEventListener("click", () => {
        const arr = self.layoutDraft.columns;
        if (idx >= arr.length - 1) return;
        const tmp = arr[idx + 1];
        arr[idx + 1] = arr[idx];
        arr[idx] = tmp;
        self.render();
      });
      tr.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", col.key);
      });
      tr.addEventListener("dragover", (e) => e.preventDefault());
      tr.addEventListener("drop", (e) => {
        e.preventDefault();
        const fromKey = e.dataTransfer.getData("text/plain");
        const toKey = col.key;
        if (!fromKey || fromKey === toKey) return;
        const keys = self.layoutDraft.columns.map((c) => c.key);
        const from = keys.indexOf(fromKey);
        const to = keys.indexOf(toKey);
        if (from < 0 || to < 0) return;
        keys.splice(to, 0, keys.splice(from, 1)[0]);
        self.setDraftOrder(keys);
        self.render();
      });
      tbody.appendChild(tr);
    });
  };

  HppvLayoutEditor.prototype._bind = function () {
    const self = this;
    if (this.autoBind.open) {
      this.els.openBtn.addEventListener("click", () => self.setMode(true));
    }
    if (this.autoBind.cancel) {
      this.els.cancelBtn.addEventListener("click", () => {
        self.layoutDraft = deepClone(self.layoutSaved);
        self.syncZebraControls();
        self.els.status.textContent = "";
        self.setMode(false);
      });
    }
    if (this.autoBind.save) {
      this.els.saveBtn.addEventListener("click", async () => {
        for (const c of self.layoutDraft.columns) {
          if (!Number.isInteger(c.width) || c.width < self.widthMin || c.width > self.widthMax) {
            self.els.status.textContent = self.messages.widthInvalid;
            return;
          }
          if (!self.isSortable(c.key) && c.sort) {
            self.els.status.textContent = self.messages.sortUnsupported;
            return;
          }
        }
        self.els.saveBtn.disabled = true;
        try {
          const saved = await self.saveFn(self.layoutDraft);
          if (!saved || !Array.isArray(saved.columns)) {
            throw new Error("HppvLayoutEditor save() must return settings with columns");
          }
          self.layoutSaved = deepClone(saved);
          self.layoutDraft = deepClone(saved);
          self.els.status.textContent = "";
          self.setMode(false);
          self.showToast(self.messages.saveOk);
          if (typeof self.onAfterSave === "function") {
            await self.onAfterSave(self.layoutSaved);
          }
        } catch (err) {
          self.els.status.textContent =
            self.messages.saveFail + (err && err.message ? "（" + err.message + "）" : "");
        } finally {
          self.els.saveBtn.disabled = false;
        }
      });
    }
    if (this.autoBind.zebra) {
      this.els.zebraColor.addEventListener("change", () => {
        self.layoutDraft.zebra_color_id = self.els.zebraColor.value;
        self.render();
      });
      this.els.zebraEnable.addEventListener("change", () => {
        self.layoutDraft.zebra_enabled = self.els.zebraEnable.checked;
        self.render();
      });
    }
  };

  HppvLayoutEditor.prototype.getSettings = function () {
    return deepClone(this.layoutSaved);
  };

  HppvLayoutEditor.prototype.replaceSettings = function (settings) {
    const next = requireObject(settings, "settings", settings);
    if (!Array.isArray(next.columns)) {
      throw new Error("HppvLayoutEditor.replaceSettings columns required");
    }
    this.layoutSaved = deepClone(next);
    this.layoutDraft = deepClone(next);
  };

  global.HppvLayoutEditor = {
    create: function (cfg) {
      return new HppvLayoutEditor(cfg);
    },
  };
})(window);
