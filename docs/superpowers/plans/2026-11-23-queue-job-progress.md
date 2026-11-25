# Queue Job-Progress Section Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A live job-progress section at the top of the queue page showing the running scan/fingerprint job's phase, counts, current file, and an ETA.

**Architecture:** `queue_view` passes `worker.status()` (with `last_scan` re-read from its own open connection) to `queue.html`, which renders the correct initial state server-side. `app.js` piggybacks on the existing 1.5s `/api/status` poll to update the section in place; the ETA is a client-side rolling rate estimate over a 30s sample window. No changes to `worker.py`, `activity.py`, or the `/api/status` payload.

**Tech Stack:** Python 3.12 / Flask / Jinja2, vanilla JS + CSS in `app/static/`, pytest.

**Spec:** `docs/superpowers/specs/2026-11-23-queue-job-progress-design.md`

## Global Constraints

- No changes to `app/worker.py`, `app/activity.py`, or the `/api/status` response shape.
- The bottom-right activity dock (`#dock` in `base.html`, its code in `app.js`) stays untouched.
- No new dependencies, no build step, no JS test harness (pytest only — client behavior is verified by the manual checklists in Tasks 3–4).
- All network (AcoustID/MusicBrainz) stays mocked in pytest; tests pass offline.
- Use existing CSS tokens (`--surface`, `--border`, `--muted`, `--accent`, `--good`, `--sp-*`, `--r-*`); text contrast ≥4.5:1; state never conveyed by color alone; the indeterminate-bar animation must respect `prefers-reduced-motion` (existing block at `style.css:838`).
- Tests: `.venv/bin/pytest -v` — all 87 existing tests plus this plan's new ones must pass.
- Jinja globals are registered in `app/__init__.py:74-76`; new ones follow that pattern.

## Review Focus

1. **Stale `last_scan` from `worker.status()`** — the worker reads a module-global DB path that is `None` until a job has run in this process, so a fresh server would show no idle line despite scans in the DB. Pinned by Task 1's idle test: `worker.status` mocked with `last_scan: None`, a scan row seeded in the DB, row must render.
2. **`queued = 0` or `fingerprinted > queued`** — would divide by zero or push the bar past 100%. Pinned by Task 3's clamp step (`pct = queued > 0 ? Math.min(100, …) : 0`) and Task 4's manual check on a fresh queue.
3. **ARIA progressbar semantics** — `aria-valuenow` must be present for a determinate bar and *omitted* for the indeterminate scanning bar (per ARIA spec). Pinned by Task 1's running test (`aria-valuenow="40"` for 1240/3100) and Task 3's manual scanning check.
4. **Duplicate screen-reader announcements** — the dock already writes `#live-status` on phase transitions; the new section must not add a second live region. Pinned by Task 1's markup test asserting exactly one `aria-live` occurrence in the queue page.
5. **Motion without opt-out** — the indeterminate bar animation must live inside the existing `prefers-reduced-motion` block. Pinned by Task 2's grep verification and Task 4's manual reduced-motion check.

---

### Task 1: Server renders the job-progress section

**Files:**
- Modify: `app/web.py` (`queue_view`, plus new `format_scan_time` helper)
- Modify: `app/__init__.py:74-76` (register the new Jinja global)
- Modify: `app/templates/queue.html` (insert section between `</header>` and `<nav id="queue-tabs">`)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `worker.status()` → dict with keys `running, phase, seen, queued, fingerprinted, errors, message, last_scan, activity`; `db.latest_scan(conn)` → dict|None with `kind, phase, seen, queued, fingerprinted, errors, status, message, started_at, finished_at`.
- Produces (Task 2/3 rely on these):
  - Template context `job`: exactly `worker.status()` output with `last_scan` replaced by `db.latest_scan(conn)` from `queue_view`'s own connection, plus two derived keys computed in `queue_view`:
    - `job["state"]`: `"running"` if `job["running"]`; else `"error"` if `phase == "error"`; else `"idle"` if `last_scan`; else `"hidden"`.
    - `job["steps"]`: `{"scan": ..., "fingerprint": ..., "done": ...}` each `"is-active" | "is-done" | "is-todo"`, mapped from phase: `scanning` → scan=`is-active`, fingerprint=`is-todo`, done=`is-todo`; `fingerprinting` → scan=`is-done`, fingerprint=`is-active`, done=`is-todo`; any other phase → all `is-done` except done=`is-active`.
  - `web.format_scan_time(epoch: float) -> str` — local-time `"HH:MM"` (`time.strftime("%H:%M", time.localtime(epoch))`), registered as Jinja global `format_scan_time`.
  - Element contract in `queue.html` (Tasks 2/3 target these exact ids/classes):

```html
<section id="job-progress" class="job-progress" data-state="{{ job.state }}"
         aria-label="Job progress"{% if job.state == 'hidden' %} hidden{% endif %}>
  <div class="job-steps" aria-hidden="true">
    <span class="job-step {{ job.steps.scan }}">✓ Scan</span>
    <span class="job-step {{ job.steps.fingerprint }}">● Fingerprinting</span>
    <span class="job-step {{ job.steps.done }}">○ Done</span>
  </div>
  <div id="job-bar" class="job-bar{% if job.state == 'running' and job.phase == 'scanning' %} is-indeterminate{% endif %}"
       role="progressbar" aria-valuemin="0" aria-valuemax="100"
       {% if job.state == 'running' and job.phase != 'scanning' and job.queued %}aria-valuenow="{{ [100, (job.fingerprinted * 100 / job.queued)|round|int].min }}"{% endif %}
       aria-label="Fingerprinting progress">
    <div id="job-bar-fill" class="job-bar-fill"{% if job.queued %} style="width: {{ [100, (job.fingerprinted * 100 / job.queued)|round|int].min }}%"{% endif %}></div>
  </div>
  <p id="job-counts" class="job-counts"></p>
  <p id="job-now" class="job-now" hidden>Now: <span id="job-now-path"></span> <span id="job-now-dur"></span></p>
  <p id="job-eta" class="job-eta" hidden></p>
  <p id="job-error" class="job-error" hidden></p>
  <p id="job-idle" class="job-idle" hidden></p>
</section>
```

  - Server fills `#job-counts`, `#job-now`, `#job-error`, `#job-idle` text from `job` (JS overwrites later):
    - counts, fingerprinting: `"{fingerprinted:,} / {queued:,} files · {errors} errors"`; scanning: `"{seen:,} files seen · {errors} errors"`; idle/error states: leave the counts/now/eta/error elements as rendered defaults (CSS hides them by `data-state`).
    - now-line: visible only when `job.state == 'running'` and `activity.current` exists — remove `hidden`, set `#job-now-path` text to `Path(path).name`, `title` attribute to the full path, `#job-now-dur` to `f"{duration}s"`.
    - error: visible when `data-state="error"` — remove `hidden`, text `f"Job failed: {job.message or 'unknown error'}"`.
    - idle: visible when `data-state="idle"` — remove `hidden`, text `f"Last scan: {format_scan_time(started_at or finished_at)} · {seen:,} files · {errors} errors"`.
  - `#job-eta` stays `hidden` server-side always (no samples yet).
  - CSS (Task 2) shows/hides `.job-steps`, `#job-bar`, `#job-counts`, `#job-now`, `#job-eta`, `#job-error`, `#job-idle` by `[data-state]` — JS only sets `data-state` and text.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web.py` (imports: `from app import db, worker`, `import re`, `import time` as needed):

```python
def _job_status(**over):
    base = {"running": False, "phase": "idle", "seen": 0, "queued": 0,
            "fingerprinted": 0, "errors": 0, "message": None,
            "last_scan": None,
            "activity": {"current": None, "recent": []}}
    base.update(over)
    return base


def _seed_scan(cfg, **fields):
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    sid = db.start_scan(conn, kind="scan")
    db.update_scan(conn, sid, **fields)
    conn.close()
    return sid


_SCAN_EPOCH = time.mktime((2026, 11, 23, 14, 5, 0, 0, 0, -1))  # local 14:05


def test_queue_job_section_hidden_without_scans(cfg, client, monkeypatch):
    monkeypatch.setattr(worker, "status", _job_status)
    html = client.get("/").data.decode()
    assert re.search(r'<section id="job-progress"[^>]*\shidden', html)
    assert 'data-state="hidden"' in html


def test_queue_job_section_idle_renders_last_scan_from_db(cfg, client, monkeypatch):
    # worker.status() claims no last_scan (global DB path unset); the DB row wins
    monkeypatch.setattr(worker, "status", _job_status)
    _seed_scan(cfg, phase="done", status="done", seen=1204, queued=300,
               fingerprinted=300, errors=12, started_at=_SCAN_EPOCH)
    html = client.get("/").data.decode()
    assert 'data-state="idle"' in html
    assert "Last scan: 14:05 · 1,204 files · 12 errors" in html


def test_queue_job_section_running_renders_counts_now_and_progress(
        cfg, client, monkeypatch):
    monkeypatch.setattr(worker, "status", _job_status(
        running=True, phase="fingerprinting", fingerprinted=1240,
        queued=3100, errors=12,
        activity={"current": {"op": "fingerprint",
                              "path": "/music/Album/04 - Track.flac",
                              "duration": 1.8},
                  "recent": []}))
    html = client.get("/").data.decode()
    assert 'data-state="running"' in html
    assert "1,240 / 3,100 files · 12 errors" in html
    assert 'aria-valuenow="40"' in html
    assert "04 - Track.flac" in html
    assert "(1.8s)" in html
    # single live region only (the dock's); no second announcement channel
    assert html.count("aria-live") == 1


def test_queue_job_section_error_shows_message(cfg, client, monkeypatch):
    monkeypatch.setattr(worker, "status", _job_status(
        phase="error", message="AcoustID HTTP 503"))
    html = client.get("/").data.decode()
    assert 'data-state="error"' in html
    assert "Job failed: AcoustID HTTP 503" in html


def test_format_scan_time_returns_local_hhmm():
    from app import web
    assert web.format_scan_time(_SCAN_EPOCH) == "14:05"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web.py -v -k "job_section or format_scan_time"`
Expected: FAIL — `data-state` not found / `web.format_scan_time` missing.

- [ ] **Step 3: Implement**

1. `app/web.py`: add `format_scan_time(epoch: float) -> str` (signature above). In `queue_view`, after the `counts` dict and **before** `conn.close()` (line 126): `job = worker.status()` then `job["last_scan"] = db.latest_scan(conn)`; compute `job["state"]` and `job["steps"]` per the Interfaces mapping; pass `job=job` in the `render_template` call.
2. `app/__init__.py`: `app.jinja_env.globals["format_scan_time"] = web.format_scan_time`.
3. `app/templates/queue.html`: insert the section markup from Interfaces after `</header>`, before the tabs `<nav>`, filling the four text elements per the Interfaces rules (Jinja conditionals on `job.state` / `job.activity.current`).

- [ ] **Step 4: Run the new tests**

Run: `.venv/bin/pytest tests/test_web.py -v -k "job_section or format_scan_time"`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -v`
Expected: all pass (87 existing + 5 new = 92).

- [ ] **Step 6: Commit**

```bash
git add app/web.py app/__init__.py app/templates/queue.html tests/test_web.py
git commit -m "feat: queue server-render job progress section"
```

---

### Task 2: Style the section

**Files:**
- Modify: `app/static/style.css` (new block after the queue/tabs styles; reduced-motion additions inside the block at line 838)

**Interfaces:**
- Consumes: the element contract from Task 1 (`#job-progress[data-state=...]`, `.job-steps`, `.job-step.is-active/.is-done/.is-todo`, `#job-bar.is-indeterminate`, `#job-bar-fill`, `#job-counts`, `#job-now`, `#job-eta`, `#job-error`, `#job-idle`).
- Produces: nothing — pure presentation. Task 3 relies on `.job-bar-fill` width being transitionable and `.job-bar.is-indeterminate` existing.

- [ ] **Step 1: Implement the styles**

Card: `.job-progress` uses `--surface` bg, `--border`, `--r-md`, `--sp-4`, `--shadow-1`, hidden by `[data-state="hidden"]`. Visibility per state (all via `[data-state]` attribute selectors): running → steps/bar/counts shown, error/idle/eta hidden as applicable; error → only `.job-error`; idle → only `.job-idle`; stepper items: `.is-todo` uses `--muted`, `.is-active` uses `--accent` + weight 600, `.is-done` uses `--good`; each step prefixed with its glyph already in the markup (✓/●/○ — never color alone). Bar: track `--chip-bg`, fill `--accent`, height ~8px, radius `999px`, `transition: width var(--dur)`. Indeterminate: `.job-bar.is-indeterminate .job-bar-fill` uses a sliding-gradient animation (infinite, ~1.2s). `#job-error` uses `--flash-error-bg`/`--flash-error-fg`; `#job-eta`/`#job-counts` use `--muted` where secondary.

- [ ] **Step 2: Verify reduced-motion coverage**

Run: `grep -n "is-indeterminate" app/static/style.css`
Expected: the animation keyframes/animation rule appear **and** are neutralized inside `@media (prefers-reduced-motion: reduce)` (line 838 block) — e.g. `animation: none` for `.job-bar.is-indeterminate .job-bar-fill`.

- [ ] **Step 3: Regression check**

Run: `.venv/bin/pytest -v`
Expected: all pass (CSS only, no behavior change).

- [ ] **Step 4: Commit**

```bash
git add app/static/style.css
git commit -m "queue: style job progress section"
```

---

### Task 3: Live updates + ETA in `app.js`

**Files:**
- Modify: `app/static/app.js` (new block after the dock code, ~line 251; one call added inside `render(data)`)

**Interfaces:**
- Consumes: the existing `render(data)` / `tick()` poll (1.5s while running), helpers `label()`, `baseName()`, `el()`; element contract from Task 1.
- Produces:
  - `renderJobProgress(data)` — called from inside `render(data)`. Sets `#job-progress` `data-state` (`running` / `error` / `idle` / `hidden`, same mapping as Task 1), updates stepper classes (same phase mapping), counts text (comma-formatted, same strings as Task 1), `#job-bar` (`aria-valuenow` + `#job-bar-fill` width when determinate; `is-indeterminate` class and **removed** `aria-valuenow` when `phase === "scanning"`), `#job-now` (from `data.activity.current`, same as server), `#job-error` / `#job-idle` text (idle line identical to Task 1's format, using `baseName`-style local formatting of `last_scan.started_at`).
  - ETA helpers:
    - module-level `etaSamples = []`, `etaPhase = null`.
    - `etaSeconds(phase, fingerprinted, queued, nowMs) -> number|null`: reset `etaSamples` when `phase !== etaPhase`; only sample when `phase === "fingerprinting"` (push `{t: nowMs, n: fingerprinted}`, drop samples older than 30000ms, cap 30). Return `null` unless the window holds ≥2 samples spanning ≥5000ms. Then `rate = (last.n - first.n) / (last.t - first.t)` files/ms; `remaining = queued - last.n`; return `null` if `rate <= 0` or `remaining <= 0`; else `remaining / rate / 1000`.
    - `formatEta(sec) -> string`: `< 60` → `"about a minute left"`; `< 3600` → `` `≈ ${Math.round(sec/60)} min left` ``; else `` `≈ ${h} hr ${m} min left` ``.
  - ETA line rules: shown only when `data-state === "running"`, `phase === "fingerprinting"`, and `etaSeconds` returns a number; otherwise `#job-eta` is `hidden`.
  - Progress clamp: `pct = queued > 0 ? Math.min(100, Math.round(fingerprinted / queued * 100)) : 0`.
  - On fetch failure in `tick()`'s `catch`: leave the section alone (server-rendered state stands). No new `aria-live` regions; announcements stay in the dock's `#live-status`.

- [ ] **Step 1: Implement `renderJobProgress` + ETA helpers**

Add the new code block and the single `renderJobProgress(data)` call at the top of `render(data)` (before the dock logic, order irrelevant). Do not modify any dock code paths.

- [ ] **Step 2: Regression check**

Run: `.venv/bin/pytest -v`
Expected: all pass (92).

- [ ] **Step 3: Manual verification — live behavior**

Run:
```bash
MUSIC_DIR=$(mktemp -d) DATA_DIR=$(mktemp -d) .venv/bin/python run.py
```
seed `$MUSIC_DIR` with a few `tests/fixtures/silence.flac` copies, open `http://127.0.0.1:8080/`, click **Scan library**. Verify:
- [ ] scanning state: indeterminate bar, no `aria-valuenow`, "N files seen" counts, no ETA line
- [ ] fingerprinting state: determinate bar grows, counts show `n / queued`, `Now:` line shows current file + duration
- [ ] ETA appears after ~6–8s of stable progress and updates; with offline/failed AcoustID it may never appear (errors instead) — acceptable, note it
- [ ] no network (fpcalc missing / AcoustID unreachable): section ends in `data-state="error"` with the message
- [ ] on page load with no job: idle line with last-scan summary

- [ ] **Step 4: Commit**

```bash
git add app/static/app.js
git commit -m "queue: live job progress updates with ETA"
```

---

### Task 4: Final verification + doc sync

**Files:**
- Modify: `AGENTS.md` (test count line only)

**Interfaces:** none — verification only.

- [ ] **Step 1: Full test suite**

Run: `.venv/bin/pytest -v`
Expected: all pass; note the exact count from the summary line.

- [ ] **Step 2: Docker smoke**

Run: `docker compose up -d --build && curl -fsS http://127.0.0.1:8080/ >/dev/null && docker compose down -v`
Expected: build succeeds, curl OK, teardown clean.

- [ ] **Step 3: Manual reduced-motion + clamp checks**

With dev server running (Task 3 Step 3 command): enable OS "reduce motion" and reload during a scan — indeterminate bar must not animate. Also load the queue with a fresh `DATA_DIR` (no scans): `queued = 0` path — section hidden, no divide-by-zero, no console errors.

- [ ] **Step 4: Update `AGENTS.md`**

Set the test count in `AGENTS.md` (line "Tests: `.venv/bin/pytest -v` (87 tests)") to the actual count from Step 1.

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md
git commit -m "docs: sync test count"
```
