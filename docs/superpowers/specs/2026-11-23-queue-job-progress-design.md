# Queue job-progress section — design

Date: 2026-11-23
Size: small change (short design)

## Intent

The queue page gives no at-a-glance view of the background job it just
kicked off. Scan/fingerprint progress lives only in the small floating
activity dock at the bottom-right. Put a live progress section at the top
of the queue so the user can see what the job is doing and roughly when
it will finish, without hunting for it.

## Goals

- A section directly above the queue tabs showing the running job.
- Live: current phase, `fingerprinted / queued` counts, error count,
  and the file currently being processed (with elapsed time).
- A "time left" estimate while fingerprinting runs.
- Idle, failed, and completed states handled gracefully.

## Non-goals / anti-goals

- No changes to `app/worker.py`, `app/activity.py`, or `/api/status`
  beyond what they already expose.
- The bottom-right dock stays as-is (it serves other pages too).
- No ETA during the scanning phase — the total file count is not known
  yet, so an estimate would be fabricated.
- No new dependencies, no build step.

## Constraints

- Data source is the existing `/api/status`, polled every 1.5s by
  `app.js` while a job is running (`tick()`).
- `queue.html` is server-rendered by Flask/Jinja; initial state should
  be correct even before JS runs (render from `worker.status()`).
- Python 3.12 / Flask / vanilla JS + CSS in `app/static/`.
- Tests: `.venv/bin/pytest -v` must stay green (87 tests); network is
  mocked.

## Approaches considered

**Placement**

- **A. New section in `queue.html` header area, driven by the existing
  poll — chosen.** Renders initial state from `worker.status()`, updated
  live by a hook in the existing `render(data)` path in `app.js`.
- B. Reuse/move the global dock — rejected: dock is fixed-position and
  shared by all pages; a pill is not a progress section.
- C. Server-rendered only, no live updates — rejected: fingerprint jobs
  take minutes; the number would be stale seconds after load.

**ETA calculation**

- **1. Client-side rolling estimate — chosen.** `app.js` buffers
  `(timestamp, fingerprinted)` samples per phase; rate = delta over a
  ~20–30s window; ETA = remaining ÷ rate. Buffer resets on phase change.
  No backend changes.
- 2. Server-side `eta_seconds` in the worker — rejected: touches the
  most concurrency-sensitive file plus its tests, to move a calculation
  only the UI consumes.

## Design

### Markup (in `app/templates/queue.html`, between `<header>` and the tabs)

A `<section id="job-progress">` with three states, driven by CSS
classes/`hidden`:

1. **Running** — phase stepper (✓ Scan → ● Fingerprinting → ○ Done),
   a progress bar (`<div role="progressbar" aria-valuenow>` with
   `fingerprinted/queued`), counts line
   (`1,240 / 3,100 files · 12 errors`), "Now:" line with current file
   path + elapsed seconds, and `≈ 6 min left`.
2. **Idle** — collapses to one line: `Last scan: <time> · N files ·
   E errors`, from `status()["last_scan"]`.
3. **Failed** — `✗ Job failed: <message>` (`phase === "error"`).

Section is `hidden` when there is no `last_scan` and no running job
(e.g. fresh install, empty DB).

### Server side (`queue_view` in `app/web.py`)

Pass `worker.status()` (or a small subset: running, phase, counts,
message, last_scan, activity.current) into the template context as
`job`. One extra call per render; `worker.status()` already opens the DB
briefly.

### Client side (`app/static/app.js`)

- Extend the existing `render(data)` (or add a sibling function called
  from it) to update `#job-progress`.
- ETA: ring buffer of samples keyed by phase; on each poll push
  `(Date.now(), fingerprinted)`; drop samples older than ~30s; when
  ≥2 samples span ≥5s, `rate = Δfingerprinted / Δt`,
  `eta = (queued − fingerprinted) / rate`. Format: `<1 min`, `≈ N min`,
  `≈ N hr M min`. Reset buffer when `data.phase` changes. Hide the ETA
  line when rate can't be computed yet (`estimating…` or hidden).
- Scanning phase: indeterminate bar (animated striped), counter shows
  `N files seen`, no ETA.
- Reuse existing helpers (`label()`, `baseName()`); no new fetch loop —
  piggyback on the poll already running.
- Update `#live-status` announcements only on phase transitions, as
  today.

### Styling (`app/static/style.css`)

- Section sits above `#queue-tabs`, full content width, uses existing
  design tokens (colors, radius, spacing scale, badge/chip styles).
- Progress bar: deterministic track + fill; indeterminate variant uses
  a subtle animation gated by `prefers-reduced-motion`.
- Contrast ≥4.5:1 for text; bar fill distinguishable from track in both
  light/dark themes; state is never conveyed by color alone (stepper
  uses ✓/●/○ glyphs + text).

### Edge cases

- Fingerprint-only job (no scan): stepper shows Scan as done from
  `last_scan`, fingerprinting active.
- Job finishes between polls: final sweep (existing `wasRunning` logic)
  renders `Done` with final counts, then the section settles to the
  idle "last scan" line on the next load.
- `queued = 0` or `fingerprinted > queued`: clamp progress to 100%.
- Fetch failure: section shows "status unavailable" (mirrors dock).

## Testing

- Template/route: `queue.html` renders `#job-progress` with
  `worker.status()` data (existing web tests extended).
- ETA math: if extracted into a testable helper, unit-test rate/ETA
  formatting and phase-reset behavior; otherwise cover via template
  assertions.
- Manual: run `docker compose up` + a scan against the fixture library,
  verify live updates, ETA appearance, reduced-motion, and the idle
  line after completion.

## What's left to the builder

Implementation of the above; all decisions are settled here.
