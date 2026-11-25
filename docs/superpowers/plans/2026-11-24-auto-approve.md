# Auto-approve & Settled-rescan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rescans skip files whose recorded state already decides the outcome (no tag re-reads), and the worker auto-approves every match — best candidate applied, pure moves organized, destinations overwritten — leaving the queue for exceptions only.

**Architecture:** Two mechanisms. (1) A new `file_state(path, mtime, size)` SQLite table acts as a settled-scan cache: the scan loop hoists stat + row lookup above `read_fields`, skips parked rows and `file_state` matches, and records state only after a fully successful read+classify. (2) `run_fingerprint_sync` gains two apply phases around its existing fingerprint loop — phase 1 moves every pending `organize-only` row, phase 2 applies the best-scoring candidate to every `awaiting-review` row with candidates — both reusing `apply_approved` and the existing per-file failure handling. `apply_approved` itself replaces its `DestinationExists` pre-check with an explicit destroy-then-move (beets 2.14.1 uniquifies paths and would otherwise silently misplace the source).

**Tech Stack:** Python 3.12 · Flask · SQLite · beets==2.14.1 · waitress · vanilla JS (existing job-progress poll)

**Spec:** `docs/superpowers/specs/2026-11-24-auto-approve-design.md` (rev. 1) — the plan argues from the spec; executors read both.

## Global Constraints

- beets pinned exactly 2.14.1; all `import beets` / `import beetsplug` only in `app/beets_adapter.py` (tests may import beets freely).
- Tests offline; AcoustID/MusicBrainz always mocked; never start `docker compose up` (the user's instance owns port 8080) — `docker build -t taggerz .` only.
- Statuses are exactly `("pending", "awaiting-review", "done", "skipped", "error")`; kinds exactly `("needs-tags", "organize-only", "unidentified", "manual")`; `proposals.path` UNIQUE (rescan stickiness).
- Scan stays read-only against audio files: it may read tags/stats and write SQLite rows (`proposals`, `scans`, `file_state`) — never touch audio.
- One writer at a time (existing `worker._lock`); `run_fingerprint_sync` is the only fingerprint entry point and now hosts both apply phases.
- `expect=(mtime, size)` mismatch raises `FileChanged`; its empty message must normalize to exactly `"file changed"` wherever it is surfaced.
- Auto-approve policy is always (no threshold); auto-applied fields are filtered to `beets_adapter.CANDIDATE_FIELDS` — never the raw candidate dict (`score`/`recommendation` would be persisted into beets.db).
- Overwrite semantics: destruction happens only immediately before `item.move()` (after tag write), so a failed write can never have destroyed the destination.
- Phase vocabulary: phase string is `"applying"` (appears twice: before and after the fingerprint loop); the stepper dict key is `"apply"`. No new dependencies; pytest-only harness — `node --check app/static/app.js` is allowed for JS syntax, no JS test infra.
- Test command from repo root: `.venv/bin/pytest -v` (109 tests at plan time; final count synced in Task 8).

## Review Focus

1. **File changes between scan and auto-apply** — must park `error: file changed` (non-empty text) and never write; owner: Task 5 (`test_apply_failure_parking_details`), Task 6 end-to-end passes `expect`.
2. **Destination exists at apply time** — must be replaced exactly (no `01 - T.1.flac` uniquified sibling), displaced beets row removed, exactly one library row left; owner: Task 2 (`test_collision_destination_overwritten`, `test_overwrite_removes_displaced_beets_row`) + Task 5 integration (`test_auto_organize_overwrites_existing_destination`).
3. **One broken file during auto-apply** — must park that row and keep going; the scan row must still reach `phase="done"`, never abandon the job; owner: Task 5 (`test_phase1_apply_failure_parks_and_continues`).
4. **Retry after a failed apply** — must not insert a second beets library row for the same path: `beets_item_id` persisted both from the success return and from `exc.item_id`; owner: Task 5 (`test_apply_failure_parking_details`), Task 6 (`test_auto_approve_persists_beets_item_id`).
5. **Settled-library rescan** — zero `read_fields` calls for parked + settled files, but an erroring file must NOT be recorded as settled and must be retried next scan; owner: Task 4 (`test_rescan_settled_library_reads_nothing`, `test_erroring_correct_file_retried_next_scan`).
6. **Proposal reopened with candidates** — `_reopen` must start a job so phase 2 picks the row up instead of it waiting for the next manual scan; owner: Task 7 (`test_retry_with_candidates_starts_job`).

---

### Task 1: `file_state` storage

**Files:**
- Modify: `app/db.py` (`_SCHEMA` at lines 22-48; new functions after `set_beets_item_id`, line 197)
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `db.connect`, `db.init_db` (existing).
- Produces (later tasks rely on these exact names):
  - `db.file_state_get(conn, path: str) -> tuple[float, int] | None` — stored `(mtime, size)` or `None`.
  - `db.file_state_set(conn, path: str, mtime: float, size: int) -> None` — upsert.
  - `db.file_state_drop(conn, path: str) -> None` — delete one entry (used after a move).
  - `db.file_state_sweep(conn, keep: set[str]) -> int` — delete every entry whose path is not in `keep`; return the number deleted.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py` (use the file's existing `_open(tmp_path)` helper):

```python
def test_file_state_set_get_roundtrip(tmp_path):
    conn = _open(tmp_path)
    db.file_state_set(conn, "/m/a.flac", 1.5, 10)
    assert db.file_state_get(conn, "/m/a.flac") == (1.5, 10)
    conn.close()


def test_file_state_get_miss_returns_none(tmp_path):
    conn = _open(tmp_path)
    assert db.file_state_get(conn, "/m/none.flac") is None
    conn.close()


def test_file_state_set_overwrites(tmp_path):
    conn = _open(tmp_path)
    db.file_state_set(conn, "/m/a.flac", 1.5, 10)
    db.file_state_set(conn, "/m/a.flac", 2.5, 20)
    assert db.file_state_get(conn, "/m/a.flac") == (2.5, 20)
    conn.close()


def test_file_state_drop_removes_entry(tmp_path):
    conn = _open(tmp_path)
    db.file_state_set(conn, "/m/a.flac", 1.5, 10)
    db.file_state_drop(conn, "/m/a.flac")
    assert db.file_state_get(conn, "/m/a.flac") is None
    db.file_state_drop(conn, "/m/never-set.flac")  # no-op, no raise
    conn.close()


def test_file_state_sweep_deletes_paths_not_in_keep(tmp_path):
    conn = _open(tmp_path)
    db.file_state_set(conn, "/m/keep.flac", 1.0, 1)
    db.file_state_set(conn, "/m/gone.flac", 2.0, 2)
    deleted = db.file_state_sweep(conn, {"/m/keep.flac"})
    assert deleted == 1
    assert db.file_state_get(conn, "/m/keep.flac") == (1.0, 1)
    assert db.file_state_get(conn, "/m/gone.flac") is None
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_db.py -v -k file_state`
Expected: 5 FAIL with `AttributeError: ... no attribute 'file_state_get'`

- [ ] **Step 3: Implement the storage in `app/db.py`**

Add to the `_SCHEMA` string:

```sql
CREATE TABLE IF NOT EXISTS file_state (
    path TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    size INTEGER NOT NULL
);
```

Add the four functions after `set_beets_item_id`. Follow the file's convention: every function takes an open `sqlite3.Connection` and ends with `conn.commit()` where it writes. `file_state_sweep` selects all stored paths, deletes those not present in `keep` (one `DELETE ... WHERE path = ?` per stale path — the caller already holds the full path set in memory), returns the delete count. `ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime, size = excluded.size` for the upsert.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_db.py -v -k file_state`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_db.py
git commit -m "feat: add file_state settled-scan cache storage"
```

---

### Task 2: Overwrite instead of `DestinationExists`

**Files:**
- Modify: `app/beets_adapter.py` (`apply_approved`, lines 194-267; add helper near it)
- Test: `tests/test_beets_adapter.py` (rewrite `test_collision_destination_never_overwritten` at line 123; add two tests)

**Interfaces:**
- Consumes: `ApplyFailed`, `FileChanged` (unchanged), `lib()`, `Item` — all already in `beets_adapter`.
- Produces: `apply_approved(path, fields, *, move_only, expect, beets_item_id) -> tuple[Path, int | None]` — same signature, now **overwrites** an existing destination; `DestinationExists` stays defined but is never raised by `apply_approved`. Private helper `_remove_library_row_at(path: Path) -> None` (module-level in `beets_adapter`).

- [ ] **Step 1: Write the failing tests**

Rewrite `test_collision_destination_never_overwritten` as `test_collision_destination_overwritten`, and append the two new tests (in `tests/test_beets_adapter.py`, which already imports `mediafile` and `pytest`):

```python
def test_collision_destination_overwritten(cfg, make_track):
    p = make_track(cfg.music_dir, "a.flac", title="T", artist="A", album="B",
                   albumartist="A", track=1)
    dest = cfg.music_dir / "A" / "B" / "01 - T.flac"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"EXISTING")
    got, item_id = beets_adapter.apply_approved(
        p, {"title": "T"}, move_only=False, expect=None, beets_item_id=None)
    assert got == dest
    assert dest.exists() and not p.exists()          # moved, not uniquified
    assert dest.read_bytes() != b"EXISTING"
    assert mediafile.MediaFile(str(dest)).title == "T"
    assert not (dest.parent / "01 - T.1.flac").exists()  # beets unique_path trap
    assert len(list(beets_adapter.lib().items())) == 1


def test_overwrite_removes_displaced_beets_row(cfg, make_track):
    import os
    p = make_track(cfg.music_dir, "a.flac", title="T", artist="A", album="B",
                   albumartist="A", track=1)
    dest = make_track(cfg.music_dir, "A/B/01 - T.flac", title="T", artist="A",
                      album="B", albumartist="A", track=1)
    displaced = beets_adapter.Item.from_path(dest)
    displaced._db = beets_adapter.lib()
    beets_adapter.lib().add(displaced)               # beets auto-assigns the id
    assert len(list(beets_adapter.lib().items())) == 1
    got, _ = beets_adapter.apply_approved(
        p, None, move_only=True, expect=None, beets_item_id=None)
    assert got == dest and dest.exists() and not p.exists()
    items = list(beets_adapter.lib().items())
    assert len(items) == 1                            # displaced row removed
    assert Path(os.fsdecode(bytes(items[0].path))) == dest
    assert mediafile.MediaFile(str(dest)).title == "T"  # source content won


def test_write_failure_leaves_existing_destination_intact(cfg, make_track,
                                                          monkeypatch):
    p = make_track(cfg.music_dir, "a.flac", title="T", artist="A", album="B",
                   albumartist="A", track=1)
    dest = make_track(cfg.music_dir, "A/B/01 - T.flac", title="T", artist="A",
                      album="B", albumartist="A", track=1)
    monkeypatch.setattr(beets_adapter.Item, "try_write",
                        lambda self, *a, **k: False)
    with pytest.raises(beets_adapter.ApplyFailed):
        beets_adapter.apply_approved(
            p, {"title": "T"}, move_only=False, expect=None, beets_item_id=None)
    # destruction is deferred to move time: a failed write never ate dest
    assert dest.exists()
    assert mediafile.MediaFile(str(dest)).title == "T"
    assert p.exists()
```

(`Path` is `pathlib.Path` — add `from pathlib import Path` at the top of the test file if absent.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_beets_adapter.py -v -k "overwrite or collision or write_failure_leaves"`
Expected: `test_collision_destination_overwritten` FAIL with `DID NOT RAISE`/`DestinationExists` raised mismatch; the other two FAIL (destination destroyed at step 3 or never replaced)

- [ ] **Step 3: Implement destroy-then-move in `app/beets_adapter.py`**

1. Delete step 3's collision check (lines 228-230, the `raise DestinationExists()` block). Keep the `DestinationExists` class defined.
2. Add module-level `_remove_library_row_at(path: Path) -> None`: iterate `lib().items()`, decode each `it.path` exactly the way step 4 already does (`os.fsdecode(bytes(...))` when bytes), and for a match call `it.remove(delete=False)` (removes the row + flex attributes, never the file; verify the keyword name against `beets/library/models.py` `Item.remove` at implementation time).
3. Replace step 6 (`if dest != path: item.move()`) with:

```python
    # 6. Move. An existing destination is replaced (spec: overwrite).
    if dest != path:
        if dest.exists():
            _remove_library_row_at(dest)
            try:
                dest.unlink()
            except OSError:
                raise ApplyFailed("failed to replace destination",
                                  item_id=item.id)
        try:
            item.move()
        except OSError:
            raise ApplyFailed("failed to move file", item_id=item.id)
```

Ordering is the design decision: destruction only after the tag write (step 5) succeeded, immediately before the move — `item.move()` then finds the path free and lands exactly at `dest` instead of beets' uniquified `dest.1`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_beets_adapter.py -v`
Expected: PASS (all — including `test_move_failure_retry_single_row`, `test_write_failure_keeps_original_and_retry_succeeds`, which must stay green)

- [ ] **Step 5: Commit**

```bash
git add app/beets_adapter.py tests/test_beets_adapter.py
git commit -m "feat: overwrite existing destination on apply instead of erroring"
```

---

### Task 3: Web cleanup for overwrite + test rewrites

**Files:**
- Modify: `app/web.py` (`_badge` lines 58-74; `approve` except at 449-456; `bulk_approve` except at 562-571)
- Test: `tests/test_web.py` (rewrite `test_queue_lists_proposals_with_badges` line 26, `test_approve_collision_surfaces_badge` line 282, `test_bulk_approve_partial_failure` line 375)

**Interfaces:**
- Consumes: Task 2's guarantee that `apply_approved` never raises `DestinationExists`.
- Produces: `_badge` returns plain `"error"` for every error row; approve/bulk catch only `(FileChanged, ApplyFailed)`.

- [ ] **Step 1: Write the failing test rewrites**

Rewrite the three tests as follows (rest of their seeding stays as-is):

```python
def test_queue_lists_proposals_with_badges(cfg, make_track, client):
    # ... existing seeding of r1 (needs-tags + candidates) and r2
    # (unidentified) unchanged; r3 seeds a generic error instead:
    db.update_status(conn, r3.id, "error", error="disk exploded")
    # ...
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"needs tags" in resp.data or b"needs-tags" in resp.data
    assert b"unidentified" in resp.data
    assert b"destination exists" not in resp.data   # badge is gone


def test_approve_collision_overwrites_destination(cfg, make_track, client):
    # same seeding as the old test (real apply, no monkeypatch): source
    # src.flac tagged T/A/B/track 1, one candidate with matching fields,
    # row awaiting-review; dest A/B/01 - T.flac pre-written b"EXISTING"
    resp = client.post(f"/proposal/{row.id}/approve",
                       data={"action": "candidate"}, follow_redirects=True)
    assert resp.status_code == 200
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    prop = db.get(conn, row.id)
    assert prop.status == "done"                    # was: error
    conn.close()
    import mediafile
    assert dest.exists() and dest.read_bytes() != b"EXISTING"
    assert mediafile.MediaFile(str(dest)).title == "T"
    resp2 = client.get("/")
    assert b"destination exists" not in resp2.data


def test_bulk_approve_collision_still_approves_everything(cfg, make_track,
                                                          client):
    # same seeding as the old test_bulk_approve_partial_failure (3 tracks,
    # one candidate each, dest A/B/01 - T1.flac pre-written b"EXISTING")
    resp = client.post("/albums/approve", data={"album_key": "A | B"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert b"approved 3" in resp.data               # was: approved 2
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    done = [r for r in db.list_all(conn) if r.status == "done"]
    err = [r for r in db.list_all(conn) if r.status == "error"]
    assert len(done) == 3 and err == []
    conn.close()
    import mediafile
    assert mediafile.MediaFile(str(dest)).title == "T1"   # replaced
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web.py -v -k "badges or collision or bulk_approve_collision"`
Expected: 3 FAIL (badge still rendered; approve parks `error: destination exists`; bulk reports `approved 2`)

- [ ] **Step 3: Remove the dead paths in `app/web.py`**

1. `_badge`: delete the `destination exists` branch (lines 62-63); error rows always return `"error"`.
2. `approve`: except tuple becomes `(beets_adapter.FileChanged, beets_adapter.ApplyFailed)`; drop the `DestinationExists` isinstance arm (keep the `FileChanged` → `str(exc) or "file changed"` normalization).
3. `bulk_approve`: same narrowing at lines 562-571.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web.py -v`
Expected: PASS (all — `test_queue_lists_proposals_with_badges` now pins the generic error badge path too)

- [ ] **Step 5: Commit**

```bash
git add app/web.py tests/test_web.py
git commit -m "feat: drop destination-exists badge and error paths (overwrite)"
```

---

### Task 4: Settled rescan (skip reads)

**Files:**
- Modify: `app/scan.py` (`run`, lines 60-130)
- Test: `tests/test_scan.py`

**Interfaces:**
- Consumes: Task 1's `db.file_state_get/set/sweep`.
- Produces: `scan.run(cfg)` semantics — per file: `stat` → `get_by_path` → parked? skip → `file_state` match? skip → read+classify → `file_state_set` → row logic (unchanged). No function-signature changes.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scan.py` (local-import `from app import beets_adapter` inside tests that spy; `_seed_tree` and `_conn` already exist):

```python
def test_rescan_settled_library_reads_nothing(cfg, make_track, monkeypatch):
    _seed_tree(cfg, make_track)
    scan.run(cfg)
    calls = []
    real = beets_adapter.read_fields
    monkeypatch.setattr(beets_adapter, "read_fields",
                        lambda p: (calls.append(str(p)), real(p))[1])
    scan.run(cfg)
    assert calls == []          # parked rows + settled correct file: no reads


def test_scan_records_file_state_for_rowless_correct_file(cfg, make_track):
    _seed_tree(cfg, make_track)
    scan.run(cfg)
    good = cfg.music_dir / "Artist" / "Album" / "01 - Song.flac"
    conn = _conn(cfg)
    st = good.stat()
    assert db.file_state_get(conn, str(good)) == (st.st_mtime, st.st_size)
    assert db.get_by_path(conn, str(good)) is None   # still no queue row
    conn.close()


def test_erroring_correct_file_retried_next_scan(cfg, make_track, monkeypatch):
    _seed_tree(cfg, make_track)
    good = cfg.music_dir / "Artist" / "Album" / "01 - Song.flac"
    calls = []
    real = beets_adapter.read_fields

    def flaky(p):
        calls.append(str(p))
        if p == good and calls.count(str(good)) == 1:
            raise RuntimeError("boom")
        return real(p)

    monkeypatch.setattr(beets_adapter, "read_fields", flaky)
    stats1 = scan.run(cfg)
    assert stats1["errors"] == 1
    conn = _conn(cfg)
    assert db.file_state_get(conn, str(good)) is None   # NOT recorded settled
    conn.close()
    stats2 = scan.run(cfg)
    assert stats2["errors"] == 0                        # retried, now succeeds
    assert calls.count(str(good)) == 2
    conn = _conn(cfg)
    assert db.file_state_get(conn, str(good)) is not None
    conn.close()


def test_parked_row_skipped_even_when_file_changes(cfg, make_track,
                                                   monkeypatch):
    _seed_tree(cfg, make_track)
    scan.run(cfg)
    conn = _conn(cfg)
    untagged = cfg.music_dir / "untagged.flac"
    row = db.get_by_path(conn, str(untagged))
    db.update_status(conn, row.id, "skipped")
    conn.close()
    os.utime(untagged, (untagged.stat().st_mtime + 100,
                        untagged.stat().st_mtime + 100))
    calls = []
    real = beets_adapter.read_fields
    monkeypatch.setattr(beets_adapter, "read_fields",
                        lambda p: (calls.append(str(p)), real(p))[1])
    scan.run(cfg)
    assert calls == []           # parked check fires before file_state/read
    conn = _conn(cfg)
    assert db.get_by_path(conn, str(untagged)).status == "skipped"
    conn.close()


def test_file_state_swept_for_deleted_files(cfg, make_track):
    _seed_tree(cfg, make_track)
    scan.run(cfg)
    conn = _conn(cfg)
    loose = str(cfg.music_dir / "loose.flac")
    assert db.file_state_get(conn, loose) is not None
    conn.close()
    (cfg.music_dir / "loose.flac").unlink()
    scan.run(cfg)
    conn = _conn(cfg)
    assert db.file_state_get(conn, loose) is None
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_scan.py -v`
Expected: the 5 new tests FAIL (settled test sees reads; `file_state_get` returns `None` where a value is expected); existing tests still PASS

- [ ] **Step 3: Restructure the loop in `app/scan.py::run`**

Final order (this is the algorithm the tests pin — line references are to current code):

1. After `audio_strs = {str(p) for p in audio_paths}` (line 72): `db.file_state_sweep(conn, audio_strs)`. The vanished-row pass (75-80) stays unchanged.
2. In the per-file loop, replace lines 82-123 with:

```python
    for p in audio_paths:
        try:
            st = p.stat()                            # hoisted (was line 95)
        except OSError:
            errors += 1                              # vanished mid-walk: counted,
            continue                                 # never crashes the scan
        mtime = float(st.st_mtime)
        size = int(st.st_size)
        row = db.get_by_path(conn, str(p))           # hoisted (was line 99)
        if row is not None and row.status in (
                STATUS_SKIPPED, STATUS_ERROR, STATUS_PENDING,
                STATUS_AWAITING_REVIEW):
            continue                                 # parked: decision made
        if db.file_state_get(conn, str(p)) == (mtime, size):
            continue                                 # settled: stat already done
        try:
            fields = beets_adapter.read_fields(p)
        except Exception:
            errors += 1
            continue                                 # no record → retried next scan
        try:
            expected = (None if missing_core(fields)
                        else beets_adapter.expected_path(p))
        except Exception:
            errors += 1
            continue
        kind = classify(fields, p, expected)
        db.file_state_set(conn, str(p), mtime, size)  # only after full success

        if row is None:
            if kind is None:
                continue
            db.enqueue(conn, path=str(p), mtime=mtime, size=size, kind=kind,
                       current_tags=fields)
            queued += 1
            continue
        if row.status == STATUS_DONE:                 # only reachable status
            if (row.mtime, row.size) == (mtime, size):
                continue
            if kind is None:
                db.update_status(conn, row.id, STATUS_DONE, mtime=mtime,
                                 size=size, current_tags=fields)
            else:
                db.update_status(conn, row.id, STATUS_PENDING, kind=kind,
                                 mtime=mtime, size=size, current_tags=fields)
                queued += 1
```

Notes: the old parked-status block after the read (lines 108-113) is now dead and removed. The `done`+unchanged equality check stays (it is the one-time settle for pre-upgrade rows without `file_state`). Imports of `STATUS_*` for the parked tuple already exist.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_scan.py -v`
Expected: PASS (all — including `test_rescan_preserves_decisions`, `test_rescan_resets_done_when_file_changed`, `test_scan_does_not_modify_files`)

- [ ] **Step 5: Commit**

```bash
git add app/scan.py tests/test_scan.py
git commit -m "feat: settled rescan skips tag reads via file_state cache"
```

---

### Task 5: Worker phase 1 — auto-organize + apply helpers

**Files:**
- Modify: `app/worker.py` (`run_fingerprint_sync` lines 39-127; two new module-level helpers; imports at lines 12-20)
- Test: `tests/test_worker.py` (5 new tests + 1 extended assertion)

**Interfaces:**
- Consumes: Task 1 (`db.file_state_set/drop`), Task 2 (`apply_approved` overwrites), existing `activity.start/finish`, `db.update_status`, `db.set_beets_item_id`.
- Produces (Task 6 relies on these exact names):
  - `_finish_apply(conn, row, dest: Path, item_id: int | None) -> None` — status `done`, persist `item_id` when not None, refresh `row.mtime/size` from a fresh stat of `dest`, upsert `file_state` for `dest`, drop the source-path `file_state` entry when the path changed; an `OSError` from the stat must be swallowed (the apply already succeeded — next scan re-records).
  - `_fail_apply(conn, row, exc: Exception) -> str` — park `error`: `FileChanged` → `str(exc) or "file changed"`; `ApplyFailed` → `str(exc)`; anything else → `f"{type(exc).__name__}: {exc}"`; persist `exc.item_id` when present; return the message.
  - Phase string `"applying"` written to `_phase` and (when `scan_id` is not None) `db.update_scan(..., phase="applying")`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_worker.py` (`scan`, `db`, `worker`, `beets_adapter`, `Path` already imported; local-import `mediafile` where needed):

```python
def test_auto_organizes_correctly_tagged_file(cfg, make_track, monkeypatch):
    p = make_track(cfg.music_dir, "loose.flac", title="Song", artist="Artist",
                   album="Album", albumartist="Artist", track=1)
    scan.run(cfg)
    fp_calls = []
    monkeypatch.setattr(worker, "recording_ids",
                        lambda path, key: fp_calls.append(str(path)) or ["rid"])
    out = worker.run_fingerprint_sync(cfg)
    dest = cfg.music_dir / "Artist" / "Album" / "01 - Song.flac"
    assert dest.exists() and not p.exists()
    assert fp_calls == []                        # organize-only never fingerprinted
    conn = _conn(cfg)
    row = db.get_by_path(conn, str(p))
    assert row.status == "done"
    st = dest.stat()
    assert db.file_state_get(conn, str(dest)) == (st.st_mtime, st.st_size)
    conn.close()
    assert out["fingerprinted"] == 1             # phase-1 applies count toward the bar


def test_auto_organize_overwrites_existing_destination(cfg, make_track,
                                                       monkeypatch):
    src = make_track(cfg.music_dir, "loose.flac", title="Song", artist="Artist",
                     album="Album", albumartist="Artist", track=1)
    make_track(cfg.music_dir, "Artist/Album/01 - Song.flac", title="Old",
               artist="Artist", album="Album", albumartist="Artist", track=1)
    scan.run(cfg)
    monkeypatch.setattr(worker, "recording_ids", lambda path, key: [])
    worker.run_fingerprint_sync(cfg)
    dest = cfg.music_dir / "Artist" / "Album" / "01 - Song.flac"
    import mediafile
    assert mediafile.MediaFile(str(dest)).title == "Song"   # replaced, not .1
    assert not src.exists()
    assert not (dest.parent / "01 - Song.1.flac").exists()


def test_phase1_apply_failure_parks_and_continues(cfg, make_track,
                                                  monkeypatch):
    make_track(cfg.music_dir, "l1.flac", title="S1", artist="A", album="B",
               albumartist="A", track=1)
    make_track(cfg.music_dir, "l2.flac", title="S2", artist="A", album="B",
               albumartist="A", track=2)
    scan.run(cfg)
    from app import beets_adapter
    calls = {"n": 0}

    def fake_apply(path, fields, *, move_only, expect, beets_item_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return (Path(str(path) + ".done"), None)

    monkeypatch.setattr(beets_adapter, "apply_approved", fake_apply)
    worker.run_fingerprint_sync(cfg)
    conn = _conn(cfg)
    rows = db.list_all(conn)
    errs = [r for r in rows if r.status == "error"]
    dones = [r for r in rows if r.status == "done"]
    assert len(errs) == 1 and "RuntimeError" in errs[0].error
    assert len(dones) == 1                       # one broken file never aborts
    assert db.latest_scan(conn)["phase"] == "done"
    conn.close()


def test_apply_failure_parking_details(cfg, make_track, monkeypatch):
    make_track(cfg.music_dir, "l1.flac", title="S1", artist="A", album="B",
               albumartist="A", track=1)
    make_track(cfg.music_dir, "l2.flac", title="S2", artist="A", album="B",
               albumartist="A", track=2)
    scan.run(cfg)
    from app import beets_adapter
    calls = {"n": 0}

    def fake_apply(path, fields, *, move_only, expect, beets_item_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise beets_adapter.FileChanged()        # carries no message
        raise beets_adapter.ApplyFailed("failed to move file", item_id=7)

    monkeypatch.setattr(beets_adapter, "apply_approved", fake_apply)
    worker.run_fingerprint_sync(cfg)
    conn = _conn(cfg)
    rows = db.list_all(conn)
    changed = [r for r in rows if r.error == "file changed"]
    failed = [r for r in rows if r.beets_item_id == 7]
    assert len(changed) == 1 and changed[0].status == "error"
    assert len(failed) == 1 and failed[0].error == "failed to move file"
    conn.close()


def test_apply_refreshes_row_expect_after_success(cfg, make_track,
                                                  monkeypatch):
    p = make_track(cfg.music_dir, "l.flac", title="S", artist="A", album="B",
                   albumartist="A", track=1)
    scan.run(cfg)
    from app import beets_adapter
    monkeypatch.setattr(beets_adapter, "apply_approved",
                        lambda path, fields, *, move_only, expect,
                        beets_item_id: (path, None))
    worker.run_fingerprint_sync(cfg)
    st = p.stat()
    conn = _conn(cfg)
    row = db.get_by_path(conn, str(p))
    assert row.status == "done"
    assert (row.mtime, row.size) == (st.st_mtime, st.st_size)  # truthful expect
    conn.close()
```

Extend the existing `test_fingerprint_persists_fingerprinting_phase` (line 170) with one more assertion after the existing two:

```python
    assert any(c.get("phase") == "applying" for c in calls)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_worker.py -v`
Expected: the 5 new tests FAIL (rows stay `pending`, no move happened, `applying` never written); the extended assertion FAILs; all pre-existing tests still PASS (no existing test leaves a pending `organize-only` row for `run_fingerprint_sync`)

- [ ] **Step 3: Implement helpers + phase 1 in `app/worker.py`**

1. Imports: add `KIND_ORGANIZE_ONLY`, `STATUS_DONE` to the `app.models` import; add `ApplyFailed`, `FileChanged` to the `app.beets_adapter` import (line 12).
2. Add `_finish_apply` and `_fail_apply` as module-level functions with the exact semantics from the Interfaces block. In `_finish_apply`, pass the fresh stat into `db.update_status(..., STATUS_DONE, mtime=..., size=...)` so the row's expect stays truthful; then `db.file_state_set(conn, str(dest), st.st_mtime, st.st_size)` and, when `str(row.path) != str(dest)`, `db.file_state_drop(conn, row.path)`.
3. In `run_fingerprint_sync`: change the pre-try `_phase = "fingerprinting"` to `_phase = "applying"`. Inside the `try`, before the `acoustid_key` line: write `phase="applying"` to the scan row (guarded by `scan_id is not None`, carrying current `_fingerprinted`/`_errors`), then loop `db.list_by_status(conn, (STATUS_PENDING,))`, skip `prop.kind != KIND_ORGANIZE_ONLY`, and per row:
   - `activity.start("move", prop.path)`
   - try: `dest, item_id = beets_adapter.apply_approved(Path(prop.path), None, move_only=True, expect=(prop.mtime, prop.size), beets_item_id=prop.beets_item_id)` → `_finish_apply(...)`, `activity.finish(ok=True, detail=f"→ {dest}")`, `_fingerprinted += 1`, scan-row update with `phase="applying"`.
   - `except (ApplyFailed, FileChanged) as exc:` → `msg = _fail_apply(conn, prop, exc)`, `activity.finish(ok=False, detail=msg)`, `_errors += 1`, scan-row update, continue.
   - `except Exception as exc:` → `traceback.print_exc()`, same as above with `detail=f"{type(exc).__name__}: {exc}"`, continue. (Per-file catch-all: one broken file must never abort the job — `worker.py:92-104` precedent.)
4. Then set `_phase = "fingerprinting"` and proceed with the existing code unchanged, except: add `if prop.kind == KIND_ORGANIZE_ONLY: continue` at the top of the fingerprint loop (defensive; phase 1 already consumed those rows).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_worker.py -v`
Expected: PASS (all — `test_manual_kind_survives_no_candidates`, `test_fingerprint_only_does_not_rescan`, `test_crashed_job_reports_error_status` stay green)

- [ ] **Step 5: Commit**

```bash
git add app/worker.py tests/test_worker.py
git commit -m "feat: worker phase 1 auto-organizes pure moves with per-file failure handling"
```

---

### Task 6: Worker phase 2 — auto-approve best match

**Files:**
- Modify: `app/worker.py` (phase 2 block after the fingerprint loop, before the final `phase="done"` write at lines 112-115; one new helper)
- Test: `tests/test_worker.py` (4 new tests + 4 rewrites)

**Interfaces:**
- Consumes: Task 5's `_finish_apply` / `_fail_apply`, `"applying"` phase writes; `beets_adapter.CANDIDATE_FIELDS`.
- Produces: `_best_candidate(cands: list[dict]) -> dict` — highest `"score"`; absent or `None` scores rank below any number (`-1.0`); ties → first (`max` is stable). Phase 2 walks **all** `awaiting-review` rows with non-empty candidates (global scope — also heals reopened/legacy rows).

- [ ] **Step 1: Write the failing tests + rewrites**

New tests for `tests/test_worker.py`:

```python
def test_auto_approves_best_match_end_to_end(cfg, make_track, monkeypatch):
    make_track(cfg.music_dir, "untagged.flac")
    scan.run(cfg)
    monkeypatch.setattr(worker, "recording_ids", lambda path, key: ["rid"])

    def cands(path, ids):
        low = {f: None for f in beets_adapter.CANDIDATE_FIELDS}
        low.update({"title": "Worse", "artist": "A", "album": "B",
                    "albumartist": "A", "track": 1, "disc": 1, "year": 2000,
                    "score": 0.4})
        high = {f: None for f in beets_adapter.CANDIDATE_FIELDS}
        high.update({"title": "Best", "artist": "A", "album": "B",
                     "albumartist": "A", "track": 1, "disc": 1, "year": 2000,
                     "score": 0.95})
        return [low, high]

    monkeypatch.setattr(worker, "candidates_for", cands)
    worker.run_scan_sync(cfg)
    conn = _conn(cfg)
    row = db.get_by_path(conn, str(cfg.music_dir / "untagged.flac"))
    assert row.status == "done"
    assert row.candidates[0]["title"] == "Worse"   # stored candidates untouched
    conn.close()
    dest = cfg.music_dir / "A" / "B" / "01 - Best.flac"
    import mediafile
    assert dest.exists()
    assert mediafile.MediaFile(str(dest)).title == "Best"


def test_auto_approve_missing_and_tied_scores_do_not_crash(cfg, make_track,
                                                           monkeypatch):
    make_track(cfg.music_dir, "untagged.flac")
    scan.run(cfg)
    monkeypatch.setattr(worker, "recording_ids", lambda path, key: ["rid"])

    def cands(path, ids):
        first = {f: None for f in beets_adapter.CANDIDATE_FIELDS}
        first.update({"title": "First", "artist": "A", "album": "B",
                      "albumartist": "A", "track": 1, "disc": 1, "year": 2000})
        # no "score" key at all — max() must not KeyError
        second = dict(first)
        second.update({"title": "Second", "score": None})  # None must not TypeError
        return [first, second]

    monkeypatch.setattr(worker, "candidates_for", cands)
    worker.run_scan_sync(cfg)
    conn = _conn(cfg)
    row = db.get_by_path(conn, str(cfg.music_dir / "untagged.flac"))
    assert row.status == "done"
    conn.close()
    assert (cfg.music_dir / "A" / "B" / "01 - First.flac").exists()
    assert not (cfg.music_dir / "A" / "B" / "01 - Second.flac").exists()


def test_auto_approve_persists_beets_item_id(cfg, make_track, monkeypatch):
    make_track(cfg.music_dir, "untagged.flac")
    scan.run(cfg)
    monkeypatch.setattr(worker, "recording_ids", lambda path, key: ["rid"])
    monkeypatch.setattr(worker, "candidates_for",
                        lambda path, ids: [{"title": "T", "score": 0.9}])
    from app import beets_adapter
    monkeypatch.setattr(beets_adapter, "apply_approved",
                        lambda path, fields, *, move_only, expect,
                        beets_item_id: (path, 42))
    worker.run_scan_sync(cfg)
    conn = _conn(cfg)
    row = db.get_by_path(conn, str(cfg.music_dir / "untagged.flac"))
    assert row.status == "done" and row.beets_item_id == 42
    conn.close()


def test_auto_approve_failure_parks_with_item_id(cfg, make_track,
                                                 monkeypatch):
    make_track(cfg.music_dir, "untagged.flac")
    scan.run(cfg)
    monkeypatch.setattr(worker, "recording_ids", lambda path, key: ["rid"])
    monkeypatch.setattr(worker, "candidates_for",
                        lambda path, ids: [{"title": "T", "score": 0.9}])
    from app import beets_adapter

    def boom(path, fields, *, move_only, expect, beets_item_id):
        raise beets_adapter.ApplyFailed("failed to move file", item_id=7)

    monkeypatch.setattr(beets_adapter, "apply_approved", boom)
    worker.run_scan_sync(cfg)
    conn = _conn(cfg)
    row = db.get_by_path(conn, str(cfg.music_dir / "untagged.flac"))
    assert row.status == "error"
    assert row.error == "failed to move file" and row.beets_item_id == 7
    assert db.latest_scan(conn)["phase"] == "done"   # job still completes
    conn.close()
```

Rewrites of the four existing tests (their current assertions no longer hold once phase 2 exists — add `monkeypatch.setattr(beets_adapter, "apply_approved", fake)` with `def fake(path, fields, *, move_only, expect, beets_item_id): return (path, None)` before the job call in each):

1. `test_scan_sync_produces_reviewable_proposals` (line 19): assert `p.status == "done"` (was `"awaiting-review"`), keep `p.candidates[0]["title"] == "T"` and `out["fingerprinted"] == 1`.
2. `test_pending_rows_are_resumed` (line 101): assert `p.status == "done"` (was `"awaiting-review"`).
3. `test_fingerprint_error_parks_proposal_and_continues` (line 62): keep the `pa` error assertions; `pb.status == "done"` (was `"awaiting-review"`).
4. `test_unexpected_error_parks_file_and_continues` (line 187): keep the `a` error assertions; `b.status == "done"` (was `"awaiting-review"`); keep the `latest_scan phase == "done"` assertion.

Note (verify green, no edit): `test_no_candidates_marks_unidentified` and `test_manual_kind_survives_no_candidates` already pin phase 2's skip of empty candidates and the fingerprint loop's inclusion of `KIND_MANUAL`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_worker.py -v`
Expected: 4 new FAIL (rows stay `awaiting-review`) + 4 rewrites FAIL (rows stay `awaiting-review` where the rewrite expects `done`)

- [ ] **Step 3: Implement phase 2 in `app/worker.py`**

1. Add `STATUS_AWAITING_REVIEW` to the `app.models` import and `CANDIDATE_FIELDS` to the `app.beets_adapter` import.
2. Add module-level `_best_candidate(cands: list[dict]) -> dict`:

```python
def _best_candidate(cands):
    """Highest-scoring candidate; absent/None scores rank below any number."""
    def _score(c):
        s = c.get("score")
        return s if isinstance(s, (int, float)) else -1.0
    return max(cands, key=_score)
```

3. After the fingerprint loop, before the final `db.update_scan(..., phase="done", status="done", ...)` write: set `_phase = "applying"`, write `phase="applying"` to the scan row (guarded by `scan_id`), then loop `db.list_by_status(conn, (STATUS_AWAITING_REVIEW,))` and skip rows with `not prop.candidates or prop.kind == KIND_ORGANIZE_ONLY`. Per row:
   - `best = _best_candidate(prop.candidates)`; `fields = {f: best.get(f) for f in CANDIDATE_FIELDS}` (raw candidates carry `score`/`recommendation` — filtering matches the approve route at `web.py:425`).
   - `activity.start("approve", prop.path)`, then the same try/except/else shape as phase 1, but calling `apply_approved(Path(prop.path), fields, move_only=False, expect=(prop.mtime, prop.size), beets_item_id=prop.beets_item_id)`; success → `_finish_apply` + `activity.finish(ok=True, detail=f"→ {dest}")` (no `_fingerprinted` bump — these rows were already counted by the fingerprint loop); failures → `_fail_apply` + continue; scan-row update with `phase="applying"` after each row.
   - Phase 2 runs regardless of `scan_id` (retry/identify jobs reach `run_fingerprint_sync` too).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_worker.py -v`
Expected: PASS (all)

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -v`
Expected: PASS (109 + new; any failure outside `test_worker.py` means a missed rewrite — fix by asserting the new auto-applied outcome, never by disabling phase 2)

- [ ] **Step 6: Commit**

```bash
git add app/worker.py tests/test_worker.py
git commit -m "feat: worker phase 2 auto-approves best candidate"
```

---

### Task 7: `applying` phase UI + `_reopen` starts a job

**Files:**
- Modify: `app/web.py` (`_step_classes` lines 85-91; `_reopen` lines 347-355)
- Modify: `app/templates/queue.html` (steps at lines 29-33; counts line 40)
- Modify: `app/static/app.js` (`LABELS` line 104, phase-transition announcements ~218-230, `jobCountsText` line 286, `activeIdx` line 349)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: phase string `"applying"` (Tasks 5/6), steps dict keys (`"apply"` is the new one).
- Produces: `_step_classes(phase) -> dict` always has keys `scan`, `fingerprint`, `apply`, `done`; `_reopen` starts a fingerprint job in both branches.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web.py` (`re`, `worker`, `_job_status` already available):

```python
def test_step_classes_applying_marks_apply_step_active():
    from app import web
    steps = web._step_classes("applying")
    assert steps == {"scan": "is-done", "fingerprint": "is-done",
                     "apply": "is-active", "done": "is-todo"}
    assert set(web._step_classes("scanning")) == set(steps)
    assert web._step_classes("scanning")["scan"] == "is-active"
    assert web._step_classes("fingerprinting")["fingerprint"] == "is-active"
    assert web._step_classes("done")["done"] == "is-active"


def test_queue_job_section_applying_renders(cfg, client, monkeypatch):
    monkeypatch.setattr(worker, "status", lambda: _job_status(
        running=True, phase="applying", fingerprinted=9, queued=10, errors=0,
        activity={"current": {"op": "move",
                              "path": "/music/A/B/01 - X.flac",
                              "duration": 0.9},
                  "recent": []}))
    html = client.get("/").data.decode()
    assert 'data-state="running"' in html
    assert re.search(r'<span class="job-step is-active">[^<]*Applying', html)
    assert 'aria-valuenow="90"' in html
    # no fake "fingerprinting" counts while applying
    assert '<p id="job-counts" class="job-counts"></p>' in html
    assert "01 - X.flac" in html                    # now-line shows the apply


def test_retry_with_candidates_starts_job(cfg, make_track, client,
                                          monkeypatch):
    p, row = _seed_proposal(cfg, make_track, candidates=[{"title": "X"}])
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    db.update_status(conn, row.id, "error", error="boom")  # candidates kept
    conn.close()
    started = {"n": 0}
    monkeypatch.setattr(worker, "start_job",
                        lambda cfg, kind="scan": started.__setitem__(
                            "n", started["n"] + 1) or True)
    resp = client.post(f"/proposal/{row.id}/retry", follow_redirects=True)
    assert resp.status_code == 200
    assert started["n"] == 1
    conn = db.connect(cfg.db_path)
    db.init_db(conn)
    prop = db.get(conn, row.id)
    assert prop.status == "awaiting-review"   # phase 2 will pick it up
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web.py -v -k "step_classes or applying or retry_with_candidates"`
Expected: 3 FAIL (`_step_classes` has no `apply` key; counts line renders fingerprint counts; `started["n"] == 0`)

- [ ] **Step 3: Implement**

1. `web._step_classes`: four-key returns —
   - `"scanning"`: scan `is-active`, rest `is-todo`
   - `"fingerprinting"`: scan `is-done`, fingerprint `is-active`, apply/done `is-todo`
   - `"applying"`: scan/fingerprint `is-done`, apply `is-active`, done `is-todo`
   - default (done/idle/error): scan/fingerprint/apply `is-done`, done `is-active`
2. `web._reopen`: add `worker.start_job(cfg, "fingerprint")` after the `set_candidates` call in the candidates branch (the else branch already starts one).
3. `queue.html`: insert `<span class="job-step {{ job.steps.apply }}">● Applying</span>` between the fingerprinting and done spans; change the counts line's `{% else %}` to `{% elif job.phase == 'fingerprinting' %}` so applying renders an empty `#job-counts`.
4. `app.js`:
   - `LABELS`: add `applying: "Applying"`.
   - phase-transition announcements (~line 220): add `else if (data.phase === "applying") { msg = "Applying changes"; }`.
   - `jobCountsText`: add `if (data.phase === "applying") return "";` before the scanning branch.
   - `activeIdx` (line 349): `scanning ? 0 : fingerprinting ? 1 : applying ? 2 : 3` (the stepper now has 4 spans).
   - No change needed: `jobState` (running wins), `etaSeconds` (already returns null outside `fingerprinting`), the progress bar (phase-1 bumps keep `fingerprinted/queued` meaningful).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web.py -v` then `node --check app/static/app.js`
Expected: pytest PASS (all — including `test_queue_job_section_running_renders_counts_now_and_progress` for the fingerprinting branch); node exits 0

- [ ] **Step 5: Commit**

```bash
git add app/web.py app/templates/queue.html app/static/app.js tests/test_web.py
git commit -m "feat: wire applying phase through job progress UI and reopen"
```

---

### Task 8: Docs sync + full suite

**Files:**
- Modify: `README.md` (lines 3-6, 15-18, 30-36, 58-65), `AGENTS.md` (Project bullet, `app/db.py` architecture bullet, test count line)
- Test: none (verification only)

**Interfaces:**
- Consumes: all prior tasks' final behavior.
- Produces: accurate docs; final test count.

- [ ] **Step 1: Run the full suite for the final count**

Run: `.venv/bin/pytest -v 2>&1 | tail -3`
Expected: all PASS; record the exact count (109 + the new tests from Tasks 1-7).

- [ ] **Step 2: Update `README.md`**

Replace the intro (lines 3-6) with:

```
A self-hosted web tool that scans a music library for missing metadata,
proposes tags via Chromaprint/AcoustID + beets, then applies the best
match automatically — writing tags and organizing files — while
anything it can't decide waits in a browser queue.
```

Replace "What it does" items 3-4 (lines 15-18) with:

```
3. **Queue** — files with no match, failed applies, and anything you
   skip wait in a browser queue grouped by album, with match scores,
   edit forms, retry, and bulk approve-per-album.
4. **Apply** — the best match is applied automatically (tags written,
   file moved into the template path); a destination that already
   exists is replaced, and a file changed on disk since the scan is
   rejected, not clobbered.
```

Replace the "Review flow" section (lines 30-36) with:

```
## Flow

Scan (settled files skipped — no tag re-reads) → fingerprint → best
match auto-applied → done
  └─ no match / apply error / your skip → queue (manual approve,
     edit, retry, bulk approve still available)
```

Replace "Safety guarantees" (lines 58-65) with:

```
## Safety guarantees

- Scans never write to audio files — they only read tags and stats.
- A file changed on disk between scan and apply is rejected
  (`file changed`), never clobbered.
- Moves replace an existing destination (the displaced copy is
  overwritten); the source file is never deleted in place — it moves.
- Failed applies leave the source at its original path, parked as an
  error, retryable from the queue.
- Anything left in the queue is written only when you approve it.
```

- [ ] **Step 3: Update `AGENTS.md`**

1. Project bullet: end with `…proposes tags via Chromaprint/AcoustID + beets, auto-applies the best match (tags + organize), and keeps exceptions in a review queue.`
2. `app/db.py` bullet: `SQLite proposal queue (sticky decisions).` → `SQLite proposal queue (sticky decisions) + file_state settled-scan cache.`
3. Tests line: replace `(109 tests)` with the count from Step 1.

- [ ] **Step 4: Run the full suite one last time**

Run: `.venv/bin/pytest -v 2>&1 | tail -1`
Expected: `N passed` where N matches the AGENTS.md count.

- [ ] **Step 5: Commit**

```bash
git add README.md AGENTS.md
git commit -m "docs: describe auto-approve and settled-rescan behavior"
```
