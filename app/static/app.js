// JobPrep UI behaviors: toasts, tabs, keyboard shortcuts, confirm dialog.
(function () {
  "use strict";

  /* ── Toasts ──
     Server emits HX-Trigger: {"toast": {"message": "…", "tone": "success"}};
     htmx dispatches that as a bubbling "toast" event with the value as detail. */
  function showToast(detail) {
    var container = document.getElementById("toasts");
    if (!container || !detail || !detail.message) return;
    var t = document.createElement("div");
    t.className = "toast" + (detail.tone === "error" ? " toast-error" : "");
    t.textContent = detail.message;
    t.addEventListener("click", function () { t.remove(); });
    container.appendChild(t);
    setTimeout(function () {
      t.classList.add("leaving");
      setTimeout(function () { t.remove(); }, 300);
    }, 4000);
  }
  document.body.addEventListener("toast", function (e) { showToast(e.detail); });

  /* ── Tabs: aria state + roving tabindex + arrow keys ── */
  function selectTab(tab) {
    var list = tab.closest('[role="tablist"]');
    if (!list) return;
    list.querySelectorAll('[role="tab"]').forEach(function (t) {
      var selected = t === tab;
      t.setAttribute("aria-selected", selected ? "true" : "false");
      t.tabIndex = selected ? 0 : -1;
    });
  }
  document.addEventListener("click", function (e) {
    var tab = e.target.closest && e.target.closest('[role="tab"]');
    if (tab) selectTab(tab);
  });
  document.addEventListener("keydown", function (e) {
    var list = e.target.closest && e.target.closest('[role="tablist"]');
    if (!list) return;
    var step = { ArrowRight: 1, ArrowLeft: -1, Home: 0, End: 0 };
    if (!(e.key in step)) return;
    var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
    var i = tabs.indexOf(document.activeElement);
    if (i === -1) return;
    e.preventDefault();
    var next = e.key === "Home" ? 0
      : e.key === "End" ? tabs.length - 1
      : (i + step[e.key] + tabs.length) % tabs.length;
    tabs[next].focus();
  });
  // Back/forward can restore a body snapshot captured mid-transition;
  // the URL's ?tab= param is the source of truth for the highlight.
  function syncTabsFromUrl() {
    var list = document.querySelector('[role="tablist"]');
    if (!list) return;
    var current = new URL(location.href).searchParams.get("tab") || "overview";
    var tab = list.querySelector('[data-tab="' + current + '"]');
    if (tab) selectTab(tab);
  }
  document.addEventListener("DOMContentLoaded", syncTabsFromUrl);
  document.body.addEventListener("htmx:historyRestore", syncTabsFromUrl);

  /* ── ⌘/Ctrl+Enter submits the containing form ── */
  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter" &&
        e.target.tagName === "TEXTAREA" && e.target.form) {
      e.preventDefault();
      e.target.form.requestSubmit();
    }
  });

  /* ── Focus [autofocus] in swapped-in content ── */
  document.body.addEventListener("htmx:afterSettle", function (e) {
    var el = e.detail.elt && e.detail.elt.querySelector
      ? e.detail.elt.querySelector("[autofocus]") : null;
    if (el) el.focus();
  });

  /* ── Dismiss: [data-dismiss="<selector>"] removes the closest match ── */
  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-dismiss]");
    if (!el) return;
    var target = el.closest(el.getAttribute("data-dismiss"));
    if (target) target.remove();
  });

  /* ── Add-fact form: show only the fields relevant to the chosen kind ── */
  function syncFactFields(root) {
    if (!root) return;
    var kind = root.querySelector("select[name=kind]");
    if (!kind) return;
    var isSkill = kind.value === "skill";
    var isDirection = kind.value === "direction";
    root.querySelectorAll("[data-fact-field=proficiency]").forEach(function (el) { el.hidden = !isSkill; });
    root.querySelectorAll("[data-fact-field=dates]").forEach(function (el) { el.hidden = isSkill || isDirection; });
  }
  document.addEventListener("change", function (e) {
    if (e.target.matches && e.target.matches("dialog select[name=kind]")) {
      syncFactFields(e.target.closest("dialog"));
    }
  });

  /* ── Add-fact dialog: switch between the describe (parse) and manual panes ── */
  function setFactMode(dialog, mode) {
    if (!dialog) return;
    dialog.querySelectorAll("[data-fact-mode]").forEach(function (b) {
      var on = b.dataset.factMode === mode;
      b.classList.toggle("active-pill", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
    dialog.querySelectorAll("[data-fact-pane]").forEach(function (p) {
      p.hidden = p.dataset.factPane !== mode;
    });
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-fact-mode]");
    if (btn) setFactMode(btn.closest("dialog"), btn.dataset.factMode);
  });

  /* ── Auto-open a dialog once HTMX swaps a form carrying
        data-autoshow-dialog="<id>" into it (the fact-edit modal). ── */
  document.body.addEventListener("htmx:afterSwap", function (e) {
    var marker = e.target.querySelector && e.target.querySelector("[data-autoshow-dialog]");
    if (!marker) return;
    var d = document.getElementById(marker.dataset.autoshowDialog);
    if (d && d.showModal && !d.open) { syncFactFields(d); d.showModal(); }
  });

  /* ── Dialog openers: [data-open-dialog="id"] / [data-close-dialog] ── */
  document.addEventListener("click", function (e) {
    var opener = e.target.closest && e.target.closest("[data-open-dialog]");
    if (opener) {
      var d = document.getElementById(opener.dataset.openDialog);
      if (d && d.showModal) {
        if (d.id === "add-fact-dialog") {
          d.querySelectorAll("form").forEach(function (f) { f.reset(); });
          setFactMode(d, "describe");
          /* Openers can pre-fill the fact (the Fit tab's gap buttons):
             data-fact-name seeds both panes, data-fact-kind the manual type. */
          var factName = opener.dataset.factName;
          if (factName) {
            var kind = d.querySelector("form[data-fact-pane=manual] select[name=kind]");
            if (kind && opener.dataset.factKind) kind.value = opener.dataset.factKind;
            var nameInput = d.querySelector("form[data-fact-pane=manual] input[name=name]");
            if (nameInput) nameInput.value = factName;
            /* The original gap requirement, kept apart from the editable name
               so the server can still check the right gap off. */
            var gapReq = d.querySelector("form[data-fact-pane=manual] input[name=gap_requirement]");
            if (gapReq) gapReq.value = factName;
            var describe = d.querySelector("form[data-fact-pane=describe] textarea[name=text]");
            if (describe) describe.value = factName + " — ";
          }
          syncFactFields(d);
        }
        d.showModal();
      }
      return;
    }
    var closer = e.target.closest && e.target.closest("[data-close-dialog]");
    if (closer) {
      var dlg = closer.closest("dialog");
      if (dlg) dlg.close();
    }
  });

  /* ── Server can close the open modal via HX-Trigger: {"close-dialog": ...} ── */
  document.body.addEventListener("close-dialog", function () {
    document.querySelectorAll("dialog[open]").forEach(function (d) {
      if (d.id !== "confirm-dialog" && d.id !== "search-dialog") d.close();
    });
  });

  /* ── Row overflow (⋯) menus: one trigger reveals a row's actions ── */
  function setRowMenu(menu, open) {
    var trigger = menu.querySelector("[data-row-menu-trigger]");
    var panel = menu.querySelector(".row-menu-panel");
    if (open) menu.setAttribute("data-open", ""); else menu.removeAttribute("data-open");
    if (trigger) trigger.setAttribute("aria-expanded", open ? "true" : "false");
    if (panel) panel.hidden = !open;
  }
  function closeRowMenus(except) {
    document.querySelectorAll("[data-row-menu][data-open]").forEach(function (m) {
      if (m !== except) setRowMenu(m, false);
    });
  }
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    var trigger = e.target.closest("[data-row-menu-trigger]");
    if (trigger) {
      var menu = trigger.closest("[data-row-menu]");
      var open = !menu.hasAttribute("data-open");
      closeRowMenus(menu);
      setRowMenu(menu, open);
      return;
    }
    if (e.target.closest(".row-menu-item")) { closeRowMenus(); return; } // action fires; collapse
    if (!e.target.closest("[data-row-menu]")) closeRowMenus();           // outside click
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeRowMenus();
  });

  /* ── Client-side sortable tables ([data-sortable] + th[data-sort]) ── */
  function cellValue(cell, type) {
    if (!cell) return type === "num" ? 0 : "";
    var raw = cell.dataset.sortValue != null ? cell.dataset.sortValue : cell.textContent.trim();
    return type === "num" ? (parseFloat(raw) || 0) : raw.toLowerCase();
  }
  document.addEventListener("click", function (e) {
    var th = e.target.closest && e.target.closest("[data-sortable] thead th[data-sort]");
    if (!th) return;
    var table = th.closest("table");
    var headers = Array.prototype.slice.call(th.parentNode.children);
    var col = headers.indexOf(th);
    var type = th.dataset.sort;
    var asc = th.getAttribute("aria-sort") !== "ascending"; // toggle; first click = ascending
    var tbody = table.tBodies[0];
    if (!tbody) return;
    Array.prototype.slice.call(tbody.rows).sort(function (a, b) {
      var av = cellValue(a.cells[col], type), bv = cellValue(b.cells[col], type);
      if (av < bv) return asc ? -1 : 1;
      if (av > bv) return asc ? 1 : -1;
      return 0;
    }).forEach(function (r) { tbody.appendChild(r); });
    headers.forEach(function (h) {
      h.setAttribute("aria-sort", "none");
      var s = h.querySelector(".sort-arrow");
      if (s) s.textContent = "";
    });
    th.setAttribute("aria-sort", asc ? "ascending" : "descending");
    var arrow = th.querySelector(".sort-arrow");
    if (arrow) arrow.textContent = asc ? "▲" : "▼";
  });

  /* ── Dropzones: filename display, drag styling, opt-in auto-submit ── */
  document.addEventListener("change", function (e) {
    var zone = e.target.closest && e.target.closest(".dropzone");
    if (!zone || e.target.type !== "file") return;
    var file = e.target.files[0];
    var name = zone.querySelector("[data-dz-name]");
    if (name) { name.textContent = file ? file.name : ""; name.hidden = !file; }
    zone.classList.toggle("has-file", !!file);
    if (file && zone.hasAttribute("data-dz-submit") && e.target.form) {
      zone.classList.add("uploading");
      e.target.form.requestSubmit();
    }
  });
  ["dragenter", "dragover"].forEach(function (type) {
    document.addEventListener(type, function (e) {
      var zone = e.target.closest && e.target.closest(".dropzone");
      if (zone) zone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach(function (type) {
    document.addEventListener(type, function (e) {
      var zone = e.target.closest && e.target.closest(".dropzone");
      if (zone) zone.classList.remove("dragover");
    });
  });

  /* ── Expanding search: collapsed to an icon until clicked ── */
  function setSearchExpanded(wrap, open) {
    wrap.classList.toggle("open", open);
    var btn = wrap.querySelector(".search-expand-btn");
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    var input = wrap.querySelector("input");
    if (input) input.tabIndex = open ? 0 : -1;
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".search-expand-btn");
    if (!btn) return;
    var wrap = btn.closest(".search-expand");
    var input = wrap.querySelector("input");
    var open = !wrap.classList.contains("open");
    setSearchExpanded(wrap, open);
    if (open) {
      input.focus();
    } else if (input.value) {
      input.value = "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
  document.addEventListener("focusout", function (e) {
    var wrap = e.target.closest && e.target.closest(".search-expand");
    if (wrap && e.target.tagName === "INPUT" && !e.target.value.trim()) {
      setSearchExpanded(wrap, false);
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    var wrap = e.target.closest && e.target.closest(".search-expand");
    if (!wrap) return;
    var input = wrap.querySelector("input");
    if (input.value) {
      input.value = "";
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    setSearchExpanded(wrap, false);
    input.blur();
  });

  /* ── Global search modal (⌘K / "/" / clicking the trigger) ── */
  function openSearch(query) {
    var dlg = document.getElementById("search-dialog");
    if (!dlg || !dlg.showModal) return;
    if (!dlg.open) dlg.showModal();
    var input = dlg.querySelector('input[name="q"]');
    if (!input) return;
    if (query != null) {
      input.value = query;
      input.dispatchEvent(new Event("input", { bubbles: true })); // fire the htmx search
    }
    input.focus();
    input.select();
  }
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    var trigger = e.target.closest(".search-trigger");
    if (trigger) { e.preventDefault(); openSearch(); return; }
    var seed = e.target.closest("[data-search]");
    if (seed) { e.preventDefault(); openSearch(seed.getAttribute("data-search")); }
  });
  document.addEventListener("keydown", function (e) {
    var isK = (e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k";
    var isSlash = e.key === "/" && !e.metaKey && !e.ctrlKey && !e.altKey;
    if (!isK && !isSlash) return;
    var t = e.target;
    if (isSlash && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    e.preventDefault();
    openSearch();
  });

  /* ── Click a dialog's backdrop (outside its content) to close ── */
  document.addEventListener("click", function (e) {
    var dlg = e.target;
    if (dlg.tagName !== "DIALOG" || !dlg.open) return;
    var r = dlg.getBoundingClientRect();
    var inside = e.clientX >= r.left && e.clientX <= r.right &&
                 e.clientY >= r.top && e.clientY <= r.bottom;
    if (!inside) dlg.close();
  });

  /* ── Facts filter (profile page): kind pills + live text search ── */
  var factsFilter = { kind: "all", query: "" };
  function applyFactsFilter() {
    var section = document.getElementById("facts-section");
    if (!section || !section.querySelector("[data-facts-kind]")) return;
    var q = factsFilter.query.trim().toLowerCase();
    var shown = 0, total = 0;
    section.querySelectorAll(".fact").forEach(function (row) {
      total++;
      if (row.querySelector("form")) { row.hidden = false; shown++; return; } // keep open editors visible
      var okKind = factsFilter.kind === "all" || row.dataset.kind === factsFilter.kind;
      var okText = !q || row.textContent.toLowerCase().indexOf(q) !== -1;
      row.hidden = !(okKind && okText);
      if (!row.hidden) shown++;
    });
    section.querySelectorAll("[data-fact-group]").forEach(function (group) {
      group.hidden = !group.querySelector(".fact:not([hidden])");
    });
    var empty = section.querySelector("[data-facts-empty]");
    if (empty) empty.hidden = shown > 0;
    var count = section.querySelector("[data-facts-count]");
    if (count) count.textContent = shown === total ? total + " facts" : shown + " of " + total + " shown";
    section.querySelectorAll("[data-facts-kind]").forEach(function (pill) {
      var active = pill.dataset.factsKind === factsFilter.kind;
      pill.classList.toggle("active-pill", active);
      pill.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }
  document.addEventListener("input", function (e) {
    if (e.target.id === "facts-search") {
      factsFilter.query = e.target.value;
      applyFactsFilter();
    }
  });
  document.addEventListener("click", function (e) {
    var pill = e.target.closest && e.target.closest("[data-facts-kind]");
    if (pill) {
      factsFilter.kind = pill.dataset.factsKind;
      applyFactsFilter();
    }
  });
  // Row edits and the post-upload refresh swap markup back in unfiltered;
  // restore the search box and re-apply the active filter.
  document.body.addEventListener("htmx:afterSwap", function () {
    var input = document.getElementById("facts-search");
    if (!input) return;
    if (input.value !== factsFilter.query) input.value = factsFilter.query;
    var wrap = input.closest(".search-expand");
    if (wrap && factsFilter.query.trim()) setSearchExpanded(wrap, true);
    applyFactsFilter();
  });
  document.addEventListener("DOMContentLoaded", applyFactsFilter);

  /* ── Insights: pill "tabs" that filter cards by kind ── */
  var insightsFilter = { kind: "all" };
  function applyInsightsFilter() {
    var area = document.getElementById("insights-area");
    if (!area) return;
    var pills = area.querySelectorAll("[data-insight-kind]");
    if (!pills.length) return;
    var counts = { all: 0 };
    area.querySelectorAll(".insight").forEach(function (card) {
      counts.all++;
      counts[card.dataset.kind] = (counts[card.dataset.kind] || 0) + 1;
    });
    if (insightsFilter.kind !== "all" && !counts[insightsFilter.kind]) insightsFilter.kind = "all";
    pills.forEach(function (pill) {
      var k = pill.dataset.insightKind;
      var n = counts[k] || 0;
      var cnt = pill.querySelector("[data-count]");
      if (cnt) cnt.textContent = n;
      if (k !== "all") pill.hidden = n === 0; // drop tabs whose section is empty
      var active = k === insightsFilter.kind;
      pill.classList.toggle("active-pill", active);
      pill.setAttribute("aria-pressed", active ? "true" : "false");
    });
    var shown = 0;
    area.querySelectorAll(".insight").forEach(function (card) {
      var ok = insightsFilter.kind === "all" || card.dataset.kind === insightsFilter.kind;
      card.hidden = !ok;
      if (ok) shown++;
    });
    var empty = area.querySelector("[data-insights-empty]");
    if (empty) empty.hidden = shown > 0;
  }
  document.addEventListener("click", function (e) {
    var pill = e.target.closest && e.target.closest("[data-insight-kind]");
    if (pill) { insightsFilter.kind = pill.dataset.insightKind; applyInsightsFilter(); }
  });
  document.body.addEventListener("htmx:afterSwap", applyInsightsFilter);
  document.addEventListener("DOMContentLoaded", applyInsightsFilter);

  /* ── Confirm dialog (replaces native confirm) ── */
  var dialog = document.getElementById("confirm-dialog");
  var messageEl = document.getElementById("confirm-message");
  var onConfirm = null;
  function openConfirm(text, fn) {
    if (!dialog || !dialog.showModal) {
      if (window.confirm(text)) fn();
      return;
    }
    messageEl.textContent = text;
    onConfirm = fn;
    dialog.showModal();
  }
  if (dialog) {
    document.getElementById("confirm-ok").addEventListener("click", function () {
      dialog.close();
      if (onConfirm) { var fn = onConfirm; onConfirm = null; fn(); }
    });
    document.getElementById("confirm-cancel").addEventListener("click", function () {
      onConfirm = null;
      dialog.close();
    });
    // When the dialog closes (OK, Cancel, Esc, backdrop), let paused rows poll again.
    dialog.addEventListener("close", function () {
      document.querySelectorAll("[data-confirm-hold]").forEach(function (el) {
        el.removeAttribute("data-confirm-hold");
      });
    });
  }
  // A self-refreshing row (hx-trigger="every …") must not swap itself out while
  // its own confirm dialog is open, or the confirmed action targets a stale
  // element and gets lost. Pause that row's polling requests until the dialog closes.
  document.body.addEventListener("htmx:beforeRequest", function (evt) {
    var el = evt.detail && evt.detail.elt;
    if (el && el.hasAttribute && el.hasAttribute("data-confirm-hold")) evt.preventDefault();
  });
  // htmx requests carrying hx-confirm
  document.body.addEventListener("htmx:confirm", function (e) {
    if (!e.detail.question) return;
    e.preventDefault();
    if (dialog && dialog.showModal && e.detail.elt.closest) {
      var poller = e.detail.elt.closest("[hx-trigger*='every']");
      if (poller) poller.setAttribute("data-confirm-hold", "");
    }
    openConfirm(e.detail.question, function () { e.detail.issueRequest(true); });
  });
  // plain forms carrying data-confirm
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.matches || !form.matches("form[data-confirm]") || form.dataset.confirmed) return;
    e.preventDefault();
    openConfirm(form.dataset.confirm, function () {
      form.dataset.confirmed = "1";
      form.submit();
    });
  }, true);

  /* ── Debounce plain (non-htmx) form submits so a double-click can't fire the
     same expensive action twice (e.g. Start interview, Add job). htmx forms show
     their own in-flight state; data-confirm forms re-submit programmatically. ── */
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || !form.matches) return;
    if (form.matches("[hx-post],[hx-get],[data-confirm]")) return;
    var btn = form.querySelector('button[type="submit"], button:not([type])');
    if (btn) setTimeout(function () { btn.disabled = true; }, 0); // after this submit dispatches
  });

  /* ── Multi-job interview picker: select-all / clear + live count ──
     Keeps the "N selected" label current and blocks starting a session with
     no jobs checked. ── */
  function syncJobPicker(picker) {
    if (!picker) return;
    var boxes = picker.querySelectorAll('input[type="checkbox"]');
    var n = 0;
    boxes.forEach(function (b) { if (b.checked) n++; });
    var count = picker.querySelector("[data-pick-count]");
    if (count) count.textContent = n + " selected";
    var form = picker.closest("form");
    var submit = form && form.querySelector("[data-pick-submit]");
    if (submit) submit.disabled = n === 0;
  }
  function setAllChecked(picker, checked) {
    picker.querySelectorAll('input[type="checkbox"]').forEach(function (b) { b.checked = checked; });
    syncJobPicker(picker);
  }
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    var all = e.target.closest("[data-pick-all]");
    if (all) { setAllChecked(all.closest("[data-job-picker]"), true); return; }
    var none = e.target.closest("[data-pick-none]");
    if (none) { setAllChecked(none.closest("[data-job-picker]"), false); }
  });
  document.addEventListener("change", function (e) {
    var picker = e.target.closest && e.target.closest("[data-job-picker]");
    if (picker && e.target.type === "checkbox") syncJobPicker(picker);
  });
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-job-picker]").forEach(syncJobPicker);
  });
})();

/* ── Display preference sync. The account (users.theme) is the source of
   truth; localStorage is only the anti-flash cache theme-boot.js reads.
   base.html stamps the stored pref on <html data-theme-server>; reconcile
   the cache to it on load, and apply Account-page saves (the server's
   theme-pref HX-Trigger event) to this tab immediately. ── */
(function () {
  function applyPref(theme) {
    try {
      if (theme === "light" || theme === "dark") {
        document.documentElement.dataset.theme = theme;
        localStorage.setItem("jobprep-theme", theme);
      } else if (theme === "system") {
        delete document.documentElement.dataset.theme;
        localStorage.removeItem("jobprep-theme");
      }
    } catch (e) {}
  }
  var server = document.documentElement.dataset.themeServer;
  if (server) applyPref(server);
  document.body.addEventListener("theme-pref", function (e) {
    if (e.detail && e.detail.theme) applyPref(e.detail.theme);
  });
})();
