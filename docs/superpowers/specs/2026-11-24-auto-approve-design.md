# Auto-approve & settled-rescan design

Date: 2026-11-24 (rev. 1 — design review fixes applied same day)
Size: project (full design)

## Intent

taggerz today is a human-in-the-loop review queue: every scan re-reads
tags for every file, fingerprinting proposes candidates, and nothing is
written until the user clicks approve. The user wants a hands-off tool:
a settled library should rescan cheaply, and every proposal the tool is
confident about should apply itself — tags written, files organized —
leaving the queue for genuine exceptions.

This design reverses two founding constraints of
`docs/superpowers/specs/2026-11-23-review-auto-tag-design.md`, explicitly
and knowingly (user decision, 2026-11-24):

1. **"No writes except through an approve action"** → the worker now
   auto-approves. Manual approve remains for whatever still queues.
2. **"Never delete, never overwrite"** → a move whose destination exists
   now overwrites the destination instead of erroring.

## Goals

1. **Settled rescan:** a rescan performs no tag reads for files whose
   recorded state already decides the outcome:
   - a proposal row parked as `skipped`/`error`/`pending`/
     `awaiting-review` → skipped (decision already made);
   - a `file_state` row (new, see below) whose `(mtime, size)` match the
     file's stat → skipped, **including files with no proposal row at
     all** (correctly-tagged, correctly-placed files — the common case in
     a settled library, and every file this tool auto-organized).
   Rescanning a settled library is stat + indexed-row-lookup only.
2. **Auto-approve tags (choice A, no threshold):** when fingerprinting
   finds any candidate for a proposal, the best-scoring candidate is
   written and the file organized automatically, immediately — no review
   step. The proposal goes straight to `done`, with the same beets
   library-row bookkeeping manual approve does (see flow below).
3. **Auto-organize:** a file whose tags are already correct
   (`kind == organize-only`) is moved to its expected path automatically,
   without fingerprinting it first (no match is needed for a pure move).
4. **Overwrite on collision:** a move whose destination exists replaces
   the destination file — explicitly (beets will *not* do this for us,
   see Approaches) — including removing the displaced file's beets
   library row.
5. **Queue becomes exceptions-only:** `unidentified` (no match found),
   read/apply errors, vanished files, and anything the user `skip`s stay
   in the queue with today's manual actions unchanged.

## Non-goals / anti-goals

- No auto-rename; no auto-retry-with-different-name.
- No confidence threshold and no threshold config (user chose "always";
  a future env knob is possible but not built now — YAGNI).
- No changes to statuses, kinds, tabs, or the review UI itself. The one
  badge change: the `destination exists` badge disappears because nothing
  raises it anymore (its `_badge` branch is removed; all other badges
  unchanged).
- The source file is never lost: moves still move (the source ends up at
  the destination path); nothing is deleted except the displaced
  destination copy.
- Scan stays **read-only against the library** — it classifies,
  enqueues, and records `file_state` only. All audio writes happen in
  the worker's apply phases.
- The mtime/size `expect` guard stays: a file edited between scan and
  apply is never silently clobbered (parks as `error: file changed`).
- Proposal rows are **not** re-pointed to the destination after a move
  (stale `done` rows at old paths already exist today after manual
  approve; unchanged). Stale `file_state` entries for deleted files are
  swept during scan (cheap, keeps the table bounded).

## Constraints (carried over unless reversed above)

- beets pinned exactly 2.14.1; all `beets`/`beetsplug` imports only in
  `app/beets_adapter.py`.
- Tests offline; AcoustID/MusicBrainz mocked.
- Rescan stickiness unchanged: UNIQUE by path; parked statuses stay
  parked; `done`+changed rows are refreshed (and re-queued only if they
  genuinely need work again — this part is already current behavior,
  `scan.py:114-123`, not a change).
- `run_fingerprint_sync` remains the only fingerprint entry; one writer
  thread at a time (existing `_lock`).
- New `file_state` table goes into `db.init_db` alongside the existing
  `CREATE TABLE` statements (verify exact init/migration shape in
  `db.py` at planning time); no backfill needed — pre-existing rows
  settle after one read.

## Approaches considered

**Where auto-apply runs**

- **A. A dedicated auto-apply step inside the existing worker job —
  chosen.** Both phases live in `run_fingerprint_sync`
  (`worker.py:39`), which every job path reaches: `run_scan_sync` calls
  it after the scan (`worker.py:152`), and fingerprint-only jobs
  (retry/identify/open-path, `web.py:342/355/517`) call it directly.
  Phase 1 (pure moves) runs before the fingerprint loop; phase 2 (best
  match) runs after it — both **before** the final
  `db.update_scan(phase="done")` write (`worker.py:112-115`), so the
  job never looks finished while applies are still pending. Reuses
  `apply_approved` and the approve route's semantics; one writer; scan
  stays read-only; failures park per-file (see flow).
- B. Apply directly inside `scan.run` — rejected: breaks the read-only
  scan invariant that tests and the spec pin, and mixes read-phase
  failures with write-phase failures.
- C. An auto-approve pass triggered by the web layer after a scan —
  rejected: needs an HTTP round-trip, and the queue should not be
  involved in routine applies.

**Overwrite mechanism**

- **1. Replace the `DestinationExists` pre-check with an explicit
  destroy-then-move — chosen.** Critical correction from review: beets
  2.14.1 does **not** replace an existing destination. `Item.move_file`
  uniquifies (`beets/library/models.py:1060-1061` →
  `beets/util/__init__.py:641-661` appends `.1`, `.2`…), so merely
  deleting the pre-check would land the source at `01 - T.1.flac` while
  `apply_approved` returned the pre-computed `dest` — silent
  misplacement. The real mechanism (see "Overwrite semantics" below):
  just before `item.move()`, if `dest != path` and `dest` exists, remove
  the displaced file's beets library row (lookup by path, then
  `Item.remove(delete=False)` — removes the row without deleting files)
  and unlink `dest`; `item.move()` then finds the path free and lands
  exactly at `dest`.
- 2. Keep the pre-check but catch it and retry with an explicit
  overwrite — rejected: two paths for one behavior, the error path rots.

**Skipping tag reads on rescan**

- **Hoist stat + row lookup above `read_fields`, and add a
  `file_state(path PRIMARY KEY, mtime, size)` cache — chosen.** Order
  per file: `stat` → `get_by_path` → parked row? skip → `file_state`
  matches stat? skip → otherwise `read_fields` + classify, then upsert
  `file_state` (only after a fully successful read+classify — erroring
  files are retried next scan) and run today's row logic. `file_state`
  is what makes the goal true for rowless correct files: without it,
  every auto-organized destination file (row stays on the vanished
  source path) would be re-read — via `read_fields` *and*
  `expected_path`, each doing its own `Item.from_path` — on every scan,
  and a settled post-tool library would cost the same as today.
- Caching read results keyed by mtime inside scan — rejected: the
  persistent `file_state` row *is* the cache; an in-process cache dies
  with the job and adds invalidation for no gain.

**Phase 2 row set**

- **All `awaiting-review` rows with non-empty candidates (global
  scope) — chosen.** `db.set_candidates` flips status to
  `awaiting-review` (`db.py:167-175`); no code path leaves a row
  `pending` with candidates, so "pending with candidates" would be an
  always-empty set. Global scope also heals legacy rows and rows
  reopened with candidates (`web.py:351-352`, which starts no job —
  see flow note). Consequence of choice A: a row a user has open in the
  browser may be auto-applied by a concurrent job; their manual approve
  then fails on the expect/status guard exactly as a double-approve
  does today. Accepted.

## Decisions (as chosen by the user)

- **Auto-approve policy: A — always.** Any candidate ⇒ best one applied
  immediately. No threshold, no review for matched files.
- **Moves auto-approve too**, including pure organize-only moves.
- **Overwrite applies everywhere a move happens** — auto and manual
  alike (user: "when the destination exists overwrite in the
  destination").
- Files whose tags are correct but sit at the wrong path are organized
  automatically on scan.

## How it works (the parts)

### Worker job flow

Runs entirely inside `run_fingerprint_sync`, under the existing lock:

```
setup (ensure_setup, connect, restore counts from latest scan row)
phase = "applying"                      # phase 1: pure moves
  for each row with status == pending AND kind == organize-only:
      apply_approved(path, None, move_only=True,
                     expect=(row.mtime, row.size),
                     beets_item_id=row.beets_item_id)
      success → status done, persist returned item_id,
                refresh file_state from a fresh stat of dest,
                drop source file_state entry, _fingerprinted += 1
      failure → park as error (see failure handling), _errors += 1
phase = "fingerprinting"
  for each pending row EXCLUDING kind == organize-only
      (i.e. needs-tags, manual, unidentified-retry — manual is
       fingerprinted exactly as today; only organize-only is excluded
       because phase 1 already consumed those):
      …today's loop unchanged (worker.py:70-111)…
phase = "applying"                      # phase 2: best match
  for each row with status == awaiting-review AND candidates non-empty:
      best = highest-scoring candidate (score handling below)
      fields = {f: best.get(f) for f in CANDIDATE_FIELDS}
      apply_approved(path, fields, move_only=False,
                     expect=(row.mtime, row.size),
                     beets_item_id=row.beets_item_id)
      success → status done, persist item_id, refresh file_state
      failure → park as error (see failure handling)
  (organize-only rows cannot reach this set: they are never
   fingerprinted, so they never get candidates)
update scan row phase="done", status="done"; _phase = "done"
```

- **Score handling:** the key is `"score"`
  (`round(1 - float(distance), 3)`, `beets_adapter.py:184-187`; pinned
  by `tests/test_beets_adapter.py:56`). It can be `None`
  (`beets_adapter.py:186-187`) and test fixtures omit it entirely, so
  select with `max(cands, key=lambda c: c["score"] if isinstance(...)`
  — absent/`None` scores rank below any number; ties → first candidate
  (`max` is stable). Never `c["score"]` directly (KeyError) and never
  compare `None` with `>` (TypeError).
- **`expect` is always passed** (both phases), taken from the row's
  stored `(mtime, size)` — stricter than manual approve, which guards
  only `awaiting-review` rows (`web.py:406-407`); intentional, the
  guard's whole point is "the file we scanned is the file we write".
- **Failure handling** mirrors the fingerprint loop's per-file
  catch-all (`worker.py:92-104`): one broken file must never abort the
  remaining applies or the job. Catch-all → park row as
  `error` with `f"{type(exc).__name__}: {exc}"`; normalize
  `FileChanged()` (which carries no message, `beets_adapter.py:209`) to
  `"file changed"` exactly as the approve route does
  (`web.py:453-454`); persist `exc.item_id` when present
  (`web.py:458-459`); `_errors += 1`; continue. Do **not** re-raise —
  an uncaught exception here would set phase `error` and abandon the
  rest of the job (`worker.py:155-179`).
- **beets bookkeeping** (was missing from rev. 0): pass
  `beets_item_id=proposal.beets_item_id`; on success persist the
  returned `item_id` via `db.set_beets_item_id` (`web.py:467-468`); on
  `ApplyFailed` persist `exc.item_id` (`web.py:458-459`). Skipping this
  re-enters apply with `beets_item_id=None` on retry → `lib().add()`
  inserts a second library row for the same path (pinned by
  `test_move_failure_retry_single_row`, `test_beets_adapter.py:185`).
- **`_reopen` gets a one-line change:** its candidates branch
  (`web.py:351-352`) sets `awaiting-review` but starts no job — under
  this design that row would wait for the next scan. Start a
  fingerprint job in both branches (safe: `start_job` returns False if
  busy).
- **Progress/counts:** phase 1 increments `_fingerprinted` per applied
  row (it is the "processed" counter the `fingerprinted/queued` bar
  measures; without this the bar can never reach 100% once
  organize-only rows are excluded from fingerprinting). Scan-row
  `phase` updates use `phase="applying"` while phases run (guarded by
  `scan_id is not None` as today).

### Overwrite semantics in `apply_approved`

- Step 3's `DestinationExists` raise (`beets_adapter.py:228-230`) is
  removed. Destructive work is **deferred to step 6, immediately before
  `item.move()`** — never at step 3, so a later tag-write failure
  cannot have already destroyed the destination:
  1. if `dest != path` and `dest` exists: find the displaced file's
     beets library row by path (exact API — `MatchQuery` vs. iterating
     `lib().items()` with `os.fsdecode` — pinned at planning) and
     `remove(delete=False)` if found; then unlink `dest`;
  2. `item.move()` — path now free, so no uniquification, file lands
     exactly at `dest`.
- Source is never at risk: expect-guard and tag-write happen before
  this, exactly as today.
- `DestinationExists` remains defined (tests/other callers may
  reference it) but is no longer raised by `apply_approved`. The
  `except DestinationExists` arms in `web.py:449-466` and `562-571`
  become dead and are removed along with the `_badge` branch
  (`web.py:62-63`). Pre-existing rows with
  `error="destination exists: …"` simply show the generic `error`
  badge after the branch removal.

### Rescan loop (scan.py) — settled rescan

```
audio_paths = walk(); audio_strs = set(…)          # as today
sweep: delete file_state rows whose path not in audio_strs
vanished-row pass over open rows: unchanged (scan.py:75-80)

for p in audio_paths:
    st = p.stat()                                   # stat hoisted (was line 95)
    row = db.get_by_path(str(p))                    # lookup hoisted (was line 99)
    if row is not None and row.status in (skipped, error, pending,
                                          awaiting-review):
        continue                                    # parked: decision made
    if file_state matches (p, st.st_mtime, st.st_size):
        continue                                    # settled: no read
    try:
        fields = read_fields(p)
        expected = None if missing_core(fields) else expected_path(p)
    except Exception:
        errors += 1; continue                       # NOT recorded → retried next scan
    kind = classify(fields, p, expected)
    upsert file_state(p, st.st_mtime, st.st_size)   # only on full success
    …row logic exactly as today (scan.py:100-123)…
```

- The `done`+unchanged equality check (`scan.py:114-116`) becomes
  redundant for settled files (the `file_state` check fires first) but
  is kept: it still runs once for pre-upgrade `done` rows without a
  `file_state` entry, after which they settle too.
- After a successful auto-apply move, `file_state` is refreshed for the
  destination in the worker (fresh stat), so the very next scan skips
  it — no read-after-move cycle.
- Behavior change vs. today, deliberate: unreadable *parked* files no
  longer increment the scan's `errors` count (they are already parked —
  the read was attempted before the parked check; the count was noise).

### Queue / UI

- No template or route changes for the queue itself: auto-applied rows
  are `done` (Done tab); exceptions use existing badges; manual and
  bulk approve stay as today except their collision case now overwrites
  (shared `apply_approved`).
- **New phase label `applying`** must be wired through everything that
  renders phases, or the UI misreports (review finding):
  - `web._step_classes` (`web.py:85-91`): branch for `applying`
    (between fingerprinting and done);
  - `app.js`: `LABELS` entry (else it renders raw lowercase),
    `activeIdx` branch (else the stepper highlights **done** while
    applying), and the job-progress `jobState`/announcement handling;
  - `queue.html`: the counts line renders only for scanning/
    fingerprinting (during `applying` it would otherwise imply
    fingerprinting is still running);
  - the server-rendered `#job-progress` first paint: `queue_view`'s
    `_step_classes` path already receives the phase string — no API
    shape change beyond the string itself.
- `activity.start(op, path)` takes free-form ops — use `"move"` /
  `"apply"` per file during the phases (matches `activity.record` in
  the approve route), so the dock and now-line show live applies.

## What changes in existing tests (plan must rewrite, not just add)

Inverts:
- `test_collision_destination_never_overwritten`
  (`tests/test_beets_adapter.py:123`) → overwrite succeeds, source
  lands exactly at `dest`, displaced beets row removed, `len(items)==1`.
- `test_approve_collision_surfaces_badge` (`tests/test_web.py:282`) →
  approve succeeds, no badge.
- `test_bulk_approve_partial_failure` (`tests/test_web.py:375-414`) →
  seeds a destination collision and asserts `approved 2` + 1 error;
  under overwrite all three succeed.
- `test_queue_lists_proposals_with_badges` (`tests/test_web.py:26-50`)
  → **rewrite, not remove**: reseed a generic error message so the
  `needs tags`/`unidentified` badge coverage it pins survives.

Add:
- Rescan: spy on `beets_adapter.read_fields` — no read for parked
  rows, none for `file_state`-matched rows (incl. a rowless correct
  file), read+upsert for new/changed files, erroring file not recorded
  and retried next scan, stale `file_state` swept.
- `run_fingerprint_sync`: organize-only rows applied (not
  fingerprinted), matched rows auto-applied to `done` with
  `beets_item_id` persisted, `KIND_MANUAL` still fingerprinted and
  surviving no-candidates (`test_manual_kind_survives_no_candidates`,
  `tests/test_worker.py:44-59`), per-file apply failure parks and
  continues, `FileChanged` gets non-empty text, score `None`/absent
  handling.
- `apply_approved`: displaced row at dest with a pre-existing beets
  library row; tag-write failure leaves dest intact (destruction is at
  move time only).
- Phase labels: `status()` reports `applying`; `_step_classes` /
  `LABELS` / `activeIdx` branches.
- `_reopen` with candidates starts a job.

## Review history

- rev. 0 reviewed2026-11-24 by fresh-context reviewer against the code.
  Critical fixes folded into this revision: overwrite mechanism was
  wrong (beets uniquifies — now explicit destroy-then-move); phase 2
  targeted an always-empty status set (now `awaiting-review`+candidates);
  fingerprint kind list stranded `KIND_MANUAL` (now "all pending except
  organize-only"); "settled rescan is stat-only" claim was false for
  rowless files (now backed by `file_state`). Important fixes: score
  key pinned with None/absent handling, `CANDIDATE_FIELDS` filtering,
  beets bookkeeping in the flow, per-file failure handling, phase
  hosting in `run_fingerprint_sync`, missed bulk-approve test rewrite.

## What's left to the builder

- Exact beets API for displaced-row lookup (`MatchQuery` vs. iteration)
  — pinned in the overwrite test.
- `file_state` exact DDL/init wiring in `db.py` + sweep query shape.
- `applying` phase copy/labels in `app.js`/`queue.html` (mechanism
  above is prescriptive; wording free).
- All test rewrites/adds listed above; full suite green offline.
