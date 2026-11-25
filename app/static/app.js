(function () {
  "use strict";

  /* ---------------- toasts (notifications) ---------------- */

  function dismissToast(el) {
    el.classList.add("toast-out");
    setTimeout(function () { el.remove(); }, 220);
  }

  document.querySelectorAll("#toasts .flash").forEach(function (el, i) {
    var isError = el.classList.contains("error");
    var close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "Dismiss notification");
    close.innerHTML =
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
    close.addEventListener("click", function () { dismissToast(el); });
    el.appendChild(close);
    // stagger entry; success messages auto-dismiss (errors wait for the user)
    setTimeout(function () { el.classList.add("toast-in"); }, 40 * i);
    if (!isError) {
      setTimeout(function () { dismissToast(el); }, 4500 + 400 * i);
    }
  });

  /* ---------------- confirm popup ---------------- */

  var dlg = document.getElementById("confirm-dialog");
  var dlgMsg = document.getElementById("confirm-msg");
  var pendingForm = null;
  var pendingSubmitter = null;

  function beginLoading(form, submitter) {
    var btn = submitter || form.querySelector('button[type="submit"], button:not([type])');
    if (!btn || btn.disabled) return;
    var label = btn.textContent;
    btn.disabled = true;
    btn.classList.add("is-loading");
    btn.textContent = "Working…";
    setTimeout(function () {
      btn.disabled = false;
      btn.classList.remove("is-loading");
      btn.textContent = label;
    }, 4000);
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;

    // second pass after the popup confirmed — proceed with loading state
    if (form.dataset.confirmed) {
      delete form.dataset.confirmed;
      beginLoading(form, e.submitter);
      return;
    }

    var msg = form.getAttribute("data-confirm");
    if (msg && dlg && typeof dlg.showModal === "function") {
      e.preventDefault();
      pendingForm = form;
      pendingSubmitter = e.submitter || null;
      dlgMsg.textContent = msg;
      dlg.returnValue = "";
      dlg.showModal();
      return;
    }
    if (msg && !window.confirm(msg)) {
      e.preventDefault();
      return;
    }
    beginLoading(form, e.submitter);
  }, true);

  if (dlg) {
    dlg.addEventListener("close", function () {
      if (dlg.returnValue === "confirm" && pendingForm) {
        pendingForm.dataset.confirmed = "1";
        pendingForm.requestSubmit(pendingSubmitter);
      } else if (pendingSubmitter && pendingSubmitter.focus) {
        pendingSubmitter.focus();
      }
      pendingForm = null;
      pendingSubmitter = null;
    });
    dlg.addEventListener("click", function (e) {
      if (e.target === dlg) dlg.close("cancel"); // backdrop click cancels
    });
  }

  /* ---------------- activity dock ---------------- */

  var dock = document.getElementById("dock");
  var pill = document.getElementById("dock-pill");
  var panel = document.getElementById("dock-panel");
  var dockText = document.getElementById("dock-text");
  var dockCount = document.getElementById("dock-count");
  var currentEl = document.getElementById("dock-current");
  var recentEl = document.getElementById("dock-recent");
  var emptyEl = document.getElementById("dock-empty");
  var liveEl = document.getElementById("live-status");

  var LABELS = {
    scan: "Scanning",
    fingerprint: "Fingerprinting",
    approve: "Approving",
    move: "Moving",
    scanning: "Scanning",
    fingerprinting: "Fingerprinting",
    done: "Done",
    error: "Failed"
  };

  function label(op) { return LABELS[op] || op || ""; }

  function baseName(p) {
    if (!p) return "";
    var parts = p.split("/");
    return parts[parts.length - 1] || p;
  }

  function fmtTime(epoch) {
    var d = new Date(epoch * 1000);
    return d.toTimeString().slice(0, 8);
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  var expanded = false;
  pill.addEventListener("click", function () {
    expanded = !expanded;
    pill.setAttribute("aria-expanded", String(expanded));
    panel.hidden = !expanded;
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && expanded && dlg && !dlg.open) {
      expanded = false;
      pill.setAttribute("aria-expanded", "false");
      panel.hidden = true;
      pill.focus();
    }
  });

  var lastPhase = null;
  var wasRunning = false;

  function render(data) {
    renderJobProgress(data);
    var act = data.activity || { current: null, recent: [] };
    var hasAny = data.running || act.current || act.recent.length > 0 ||
                 data.phase === "error";
    dock.hidden = !hasAny;
    pill.setAttribute("aria-busy", data.running ? "true" : "false");
    dock.classList.toggle("is-error", data.phase === "error");

    // pill text: current file first, then phase + counts
    if (data.phase === "error") {
      dockText.textContent = "Failed: " + (data.message || "unknown error");
      dockCount.textContent = "";
    } else if (act.current) {
      dockText.textContent = label(act.current.op) + " · " + baseName(act.current.path);
      dockCount.textContent = data.running
        ? data.fingerprinted + "/" + data.queued
        : "";
    } else if (data.running) {
      dockText.textContent = label(data.phase) + "…";
      dockCount.textContent = data.fingerprinted + "/" + data.queued;
    } else {
      dockText.textContent = "Recent activity";
      dockCount.textContent = "";
    }

    // current operation panel row
    currentEl.textContent = "";
    if (act.current) {
      currentEl.hidden = false;
      var spin = el("span", "dock-spin small");
      spin.setAttribute("aria-hidden", "true");
      currentEl.appendChild(spin);
      currentEl.appendChild(el("b", null, label(act.current.op)));
      var cp = el("span", "act-path", act.current.path || "");
      cp.title = act.current.path || "";
      currentEl.appendChild(cp);
      if (act.current.duration != null) {
        currentEl.appendChild(el("span", "act-dur", act.current.duration + "s"));
      }
    } else {
      currentEl.hidden = true;
    }

    // recent list (newest first, cap display at 15)
    recentEl.textContent = "";
    var recent = (act.recent || []).slice(0, 15);
    emptyEl.hidden = recent.length > 0 || !!act.current;
    recent.forEach(function (ev) {
      var li = el("li", ev.ok ? "act-ok" : "act-fail");
      li.appendChild(el("span", "act-time", fmtTime(ev.at)));
      li.appendChild(el("span", "act-op", label(ev.op)));
      var p = el("span", "act-path", ev.path || "");
      p.title = ev.path || "";
      li.appendChild(p);
      li.appendChild(el("span", "act-status", ev.ok ? "done" : "failed"));
      if (ev.detail) {
        var d = el("span", "act-detail", ev.detail);
        d.title = ev.detail;
        li.appendChild(d);
      }
      recentEl.appendChild(li);
    });

    // contextual atomic announcements on phase transitions only
    if (data.phase !== lastPhase) {
      var msg = "";
      if (data.phase === "scanning") msg = "Scan started";
      else if (data.phase === "fingerprinting") {
        msg = "Fingerprinting started, " + data.queued + " files queued";
      } else if (data.phase === "done") {
        msg = "Scan complete: " + data.fingerprinted + " fingerprinted, " +
              data.errors + " errors";
      } else if (data.phase === "error") {
        msg = "Job failed: " + (data.message || "unknown error");
      }
      if (msg) liveEl.textContent = msg;
      lastPhase = data.phase;
    }
  }

  async function tick() {
    try {
      var res = await fetch("/api/status");
      var data = await res.json();
      render(data);
      if (data.running) {
        wasRunning = true;
        setTimeout(tick, 1500);
      } else if (wasRunning) {
        // one final sweep after a job ends to capture completion state
        wasRunning = false;
        setTimeout(tick, 2500);
      }
    } catch (e) {
      dockText.textContent = "status unavailable";
      dock.hidden = false;
    }
  }
  tick();

  /* ---------------- queue job progress (top of queue page) ---------------- */

  var jobSection = document.getElementById("job-progress");
  var jobBar = document.getElementById("job-bar");
  var jobFill = document.getElementById("job-bar-fill");
  var jobCounts = document.getElementById("job-counts");
  var jobNow = document.getElementById("job-now");
  var jobNowPath = document.getElementById("job-now-path");
  var jobNowDur = document.getElementById("job-now-dur");
  var jobEta = document.getElementById("job-eta");
  var jobError = document.getElementById("job-error");
  var jobIdle = document.getElementById("job-idle");
  var jobSteps = jobSection
    ? jobSection.querySelectorAll(".job-step")
    : [];

  var etaSamples = [];
  var etaPhase = null;
  // state the server rendered on first paint; used when /api/status has no
  // last_scan yet on a fresh process (worker DB path not set until a job runs)
  var jobInitialState = jobSection
    ? jobSection.getAttribute("data-state")
    : null;

  function jobState(data) {
    if (data.running) return "running";
    if (data.phase === "error") return "error";
    if (data.last_scan) return "idle";
    if (jobInitialState === "idle") return "idle";
    return "hidden";
  }

  function jobCountsText(data) {
    if (data.phase === "scanning") {
      return data.seen.toLocaleString() + " files seen · " +
             data.errors + " errors";
    }
    return data.fingerprinted.toLocaleString() + " / " +
           data.queued.toLocaleString() + " files · " +
           data.errors + " errors";
  }

  function jobIdleText(scan) {
    var t = scan.started_at || scan.finished_at;
    var d = new Date(t * 1000);
    var hhmm = String(d.getHours()).padStart(2, "0") + ":" +
               String(d.getMinutes()).padStart(2, "0");
    return "Last scan: " + hhmm + " · " +
           Number(scan.seen || 0).toLocaleString() + " files · " +
           (scan.errors || 0) + " errors";
  }

  // Rolling ETA: rate over a ~30s window of fingerprinted-count samples,
  // reset whenever the phase changes. Returns seconds, or null.
  function etaSeconds(phase, fingerprinted, queued, nowMs) {
    if (phase !== etaPhase) {
      etaSamples = [];
      etaPhase = phase;
    }
    if (phase !== "fingerprinting") return null;
    etaSamples.push({ t: nowMs, n: fingerprinted });
    while (etaSamples.length > 1 && nowMs - etaSamples[0].t > 30000) {
      etaSamples.shift();
    }
    if (etaSamples.length < 2) return null;
    var first = etaSamples[0];
    var last = etaSamples[etaSamples.length - 1];
    var span = last.t - first.t;
    if (span < 5000) return null;
    var rate = (last.n - first.n) / span; // files per ms
    if (!(rate > 0)) return null;
    var remaining = queued - last.n;
    if (remaining <= 0) return null;
    return remaining / rate / 1000;
  }

  function formatEta(sec) {
    if (sec < 60) return "about a minute left";
    if (sec < 3600) return "≈ " + Math.round(sec / 60) + " min left";
    var h = Math.floor(sec / 3600);
    var m = Math.round((sec % 3600) / 60);
    return "≈ " + h + " hr " + m + " min left";
  }

  function renderJobProgress(data) {
    if (!jobSection) return;
    var state = jobState(data);
    jobSection.setAttribute("data-state", state);
    if (state === "hidden") {
      jobSection.hidden = true;
      return;
    }
    jobSection.hidden = false;

    // stepper: scan / fingerprint / done
    var activeIdx = data.phase === "scanning" ? 0
                  : data.phase === "fingerprinting" ? 1 : 2;
    for (var i = 0; i < jobSteps.length; i++) {
      var cls = i < activeIdx ? "is-done"
              : i === activeIdx ? "is-active" : "is-todo";
      jobSteps[i].className = "job-step " + cls;
    }

    if (state === "running") {
      var indeterminate = data.phase === "scanning";
      jobBar.classList.toggle("is-indeterminate", indeterminate);
      if (indeterminate) {
        jobBar.removeAttribute("aria-valuenow");
        jobFill.style.width = ""; // CSS animation drives the fill
      } else {
        var pct = data.queued > 0
          ? Math.min(100, Math.round(data.fingerprinted * 100 / data.queued))
          : 0;
        jobBar.setAttribute("aria-valuenow", String(pct));
        jobFill.style.width = pct + "%";
      }
      jobCounts.textContent = jobCountsText(data);

      var cur = data.activity && data.activity.current;
      if (cur && cur.path) {
        jobNow.hidden = false;
        jobNowPath.textContent = baseName(cur.path);
        jobNowPath.title = cur.path || "";
        jobNowDur.textContent = "(" + cur.duration + "s)";
      } else {
        jobNow.hidden = true;
      }

      var eta = etaSeconds(data.phase, data.fingerprinted, data.queued,
                           Date.now());
      if (data.phase === "fingerprinting" && eta != null) {
        jobEta.hidden = false;
        jobEta.textContent = formatEta(eta);
      } else {
        jobEta.hidden = true;
      }
      jobError.hidden = true;
      jobIdle.hidden = true;
    } else if (state === "error") {
      jobError.hidden = false;
      jobError.textContent = "Job failed: " + (data.message || "unknown error");
      jobEta.hidden = true;
      jobNow.hidden = true;
      jobIdle.hidden = true;
    } else if (state === "idle") {
      jobIdle.hidden = false;
      // on a fresh process status() may have no last_scan yet — keep the
      // server-rendered line rather than blanking it
      if (data.last_scan) jobIdle.textContent = jobIdleText(data.last_scan);
      jobEta.hidden = true;
      jobError.hidden = true;
      jobNow.hidden = true;
    }
  }

  /* ---------------- queue: search + collapse (large libraries) ---------------- */

  var searchInput = document.getElementById("queue-search");
  var searchForm = document.getElementById("queue-search-form");
  if (searchInput) {
    var groupEls = Array.prototype.slice.call(document.querySelectorAll(".group"));
    var qGroups = groupEls.map(function (g) {
      return {
        el: g,
        key: g.getAttribute("data-group") || "",
        table: g.querySelector(".queue-table"),
        toggle: g.querySelector(".group-toggle"),
        rows: Array.prototype.slice.call(g.querySelectorAll("tbody tr"))
      };
    });
    var toggleAll = document.getElementById("toggle-all");

    // progressive enhancement: mute the directory, emphasize the filename
    qGroups.forEach(function (g) {
      g.rows.forEach(function (row) {
        var a = row.querySelector(".path");
        if (!a) return;
        var full = a.textContent;
        var i = full.lastIndexOf("/");
        if (i <= 0) return;
        a.textContent = "";
        var d = document.createElement("span");
        d.className = "dir";
        d.textContent = full.slice(0, i + 1);
        var f = document.createElement("b");
        f.className = "file";
        f.textContent = full.slice(i + 1);
        a.appendChild(d);
        a.appendChild(f);
      });
    });

    // collapse state, persisted; large libraries start collapsed for overview
    var STORAGE_KEY = "taggerz.collapsed";
    var collapsed = new Set();
    try {
      var stored = localStorage.getItem(STORAGE_KEY);
      if (stored) {
        collapsed = new Set(JSON.parse(stored));
      } else if (qGroups.length > 10) {
        qGroups.forEach(function (g) { collapsed.add(g.key); });
      }
    } catch (e) { /* storage unavailable — default to expanded */ }

    function persistCollapsed() {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(Array.from(collapsed)));
      } catch (e) { /* ignore */ }
    }

    function setCollapsed(g, isCollapsed) {
      g.table.hidden = isCollapsed;
      if (g.toggle) g.toggle.setAttribute("aria-expanded", String(!isCollapsed));
    }

    function anyExpanded() {
      return qGroups.some(function (g) { return !g.table.hidden; });
    }

    function updateToggleAll() {
      if (!toggleAll) return;
      toggleAll.textContent = anyExpanded() ? "Collapse all" : "Expand all";
    }

    function applyCollapseState() {
      qGroups.forEach(function (g) { setCollapsed(g, collapsed.has(g.key)); });
      updateToggleAll();
    }

    qGroups.forEach(function (g) {
      if (!g.toggle) return;
      g.toggle.addEventListener("click", function () {
        if (collapsed.has(g.key)) collapsed.delete(g.key);
        else collapsed.add(g.key);
        persistCollapsed();
        applyCollapseState();
      });
    });

    if (toggleAll) {
      toggleAll.addEventListener("click", function () {
        collapsed = new Set();
        if (!anyExpanded()) {
          qGroups.forEach(function (g) { collapsed.add(g.key); });
        }
        persistCollapsed();
        applyCollapseState();
      });
    }

    // search is server-side (pagination would cripple a client filter);
    // debounce into a GET submit, autofocus keeps typing alive across reloads
    if (searchForm) {
      var initialQ = searchInput.value;
      var searchTimer;
      searchInput.addEventListener("input", function () {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(function () {
          if (searchInput.value !== initialQ) searchForm.submit();
        }, 350);
      });
    }

    applyCollapseState();
  }
})();
