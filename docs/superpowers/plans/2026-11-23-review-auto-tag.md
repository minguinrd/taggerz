# Review & Auto-Tag Music Library (taggerz) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-hosted Flask tool that scans a music library for missing metadata, proposes tags via Chromaprint/AcoustID + beets, lets the user approve each change in a web UI, then writes tags and organizes approved files — never touching a file without approval.

**Architecture:** One Docker container, one Python process. Flask serves a server-rendered review queue backed by a SQLite proposals table. All beets interaction (candidate matching, tag writing, path templating, library DB) lives behind a single adapter module. A background worker thread walks the library, fingerprints gap files, and moves proposals to `awaiting-review`.

**Tech Stack:** Python 3.12 · Flask · waitress · beets==2.14.1 (`beets[chroma]` → pyacoustid) · Chromaprint `fpcalc` · SQLite · Docker

**Spec:** `docs/superpowers/specs/2026-11-23-review-auto-tag-design.md` — the plan argues from the spec; executors read both.

## Global Constraints

- **No writes to audio files except through an approve action.** Scan and fingerprint phases are read-only against the library.
- **Never delete, never overwrite.** An approve whose destination exists raises `DestinationExists`; the proposal stays open with a `destination exists` badge. No auto-rename.
- **beets==2.14.1 pinned exactly.** Every `import beets…` / `import beetsplug…` occurs only in `app/beets_adapter.py`. `app/fingerprint.py` may import only `acoustid` (pyacoustid). Tests may import beets freely.
- **Path template:** `$albumartist/$album/$track - $title`, applied to beets config keys `paths.default`, `paths.singleton`, `paths.comp`.
- **Statuses:** `pending` → `awaiting-review` → `done` | `skipped` | `error` (string constants in `app/models.py`).
- **Kinds:** `needs-tags`, `organize-only`, `unidentified`, `manual`.
- **Env vars:** `MUSIC_DIR` (default `/music`), `DATA_DIR` (default `/data`), `PORT` (default `8080`), `HOST` (default `0.0.0.0`), `ACOUSTID_APP_KEY` (optional; default is beets' embedded application key).
- **Python 3.12** everywhere: `.venv` on host (via uv), `python:3.12-slim` in Docker. Tests must pass offline — network (AcoustID/MusicBrainz) is always mocked in pytest.
- **Rescan stickiness:** proposals keyed UNIQUE by path; `skipped` stays skipped, `error` stays parked, `done`+unchanged untouched; rescan enqueues only new files and resets `done` files whose (mtime, size) changed.
- **All POST routes are HTML form posts** that redirect + flash. The only JSON endpoint is `GET /api/status`. No auth; compose binds `127.0.0.1:8080`.

## Review Focus

Five failure modes the spec implies that ordinary happy-path tests miss. Each is pinned by a test in the task named.

1. **Approve must never overwrite an existing file** (duplicate libraries routinely produce colliding destinations). → Task 6 `test_collision_destination_never_overwritten`, Task 9 `test_approve_collision_surfaces_badge`.
2. **A file changed on disk between scan and approve must never be silently overwritten.** → Task 6 `test_file_changed_rejected`, Task 9 `test_approve_with_changed_mtime_errors`.
3. **A failed apply must leave a consistent, retryable state** — file at its original path, no duplicate beets-library rows on retry, tags not half-committed to the queue. → Task 6 `test_write_failure_keeps_original_and_retry_succeeds`, `test_move_failure_retry_single_row`.
4. **Rescan must never re-ask a decision** (skipped/error/done proposals untouched; only new files and changed-done files re-enter). → Task 4 `test_rescan_preserves_decisions`, `test_rescan_resets_done_when_file_changed`.
5. **Scan must not modify library files in any way** (the anti-goal that makes the tool trustworthy). → Task 4 `test_scan_does_not_modify_files`.

---

### Task 1: Toolchain, scaffolding, config

**Files:**
- Create: `requirements.txt`, `pytest.ini`, `.gitignore`, `app/__init__.py`, `app/config.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `app.config.Config` — frozen dataclass with fields `music_dir: Path`, `data_dir: Path`, `db_path: Path`, `acoustid_app_key: str | None`, `host: str`, `port: int`.
- Produces: `app.config.load_config(environ: Mapping[str, str] = os.environ) -> Config` — pure, touches no filesystem. `db_path` is always `data_dir / "queue.db"`.

- [x] **Step 1: Bootstrap toolchain**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
$HOME/.local/bin/uv venv --python 3.12 .venv
$HOME/.local/bin/uv pip install --python .venv/bin/python flask waitress "beets[chroma]==2.14.1" pyacoustid pytest
$HOME/.local/bin/uv pip freeze --python .venv/bin/python > requirements.txt
```
Expected: `.venv/bin/python --version` → `3.12.x`; `requirements.txt` contains `beets==2.14.1`.

- [x] **Step 2: Write the failing config tests** — `tests/test_config.py`:

```python
def test_defaults(monkeypatch):
    for var in ("MUSIC_DIR", "DATA_DIR", "PORT", "HOST", "ACOUSTID_APP_KEY"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config(environ={})
    assert cfg.music_dir == Path("/music")
    assert cfg.data_dir == Path("/data")
    assert cfg.db_path == Path("/data/queue.db")
    assert cfg.acoustid_app_key is None
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 8080

def test_env_overrides():
    cfg = load_config(environ={"MUSIC_DIR": "/m", "DATA_DIR": "/d",
                               "PORT": "9000", "HOST": "127.0.0.1",
                               "ACOUSTID_APP_KEY": "abc"})
    assert cfg.music_dir == Path("/m")
    assert cfg.db_path == Path("/d/queue.db")
    assert cfg.port == 9000
    assert cfg.acoustid_app_key == "abc"

def test_bad_port_rejected():
    with pytest.raises(ValueError):
        load_config(environ={"PORT": "not-a-port"})
```

- [x] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.config'`

- [x] **Step 4: Implement `app/config.py`**

Frozen dataclass `Config`; `load_config` reads the five env vars with the defaults above, `int()`-parses PORT (ValueError propagates), returns `Config`. Create `app/__init__.py` as an empty package marker.

- [x] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/test_config.py -v` → 3 passed.

- [x] **Step 6: Scaffold repo and commit**

Write `pytest.ini` (`[pytest] testpaths = tests`), `.gitignore` (`.venv/`, `__pycache__/`, `*.pyc`, `music/`, `/data/`), then:

```bash
git init
git add -A
git commit -m "chore: scaffold repo, toolchain, and config loader"
```

---

### Task 2: Proposal queue (SQLite)

**Files:**
- Create: `app/models.py`, `app/db.py`, `tests/test_db.py`

**Interfaces:**
- Produces: `app/models.py` — `STATUSES = ("pending", "awaiting-review", "done", "skipped", "error")`, `KINDS = ("needs-tags", "organize-only", "unidentified", "manual")`, and `@dataclass Proposal` with fields: `id: int`, `path: str`, `mtime: float`, `size: int`, `kind: str`, `status: str`, `current_tags: dict`, `candidates: list[dict]`, `error: str | None`, `beets_item_id: int | None`.
- Produces (all take an open `sqlite3.Connection` as first arg; `connect(db_path: Path) -> sqlite3.Connection` sets `row_factory` and foreign keys):
  - `init_db(conn) -> None`
  - `enqueue(conn, *, path: str, mtime: float, size: int, kind: str, current_tags: dict) -> Proposal` — `INSERT … ON CONFLICT(path) DO NOTHING`, then returns the row (existing row returned as-is: status and decisions preserved).
  - `get(conn, pid: int) -> Proposal | None`, `get_by_path(conn, path: str) -> Proposal | None`
  - `list_by_status(conn, statuses: Sequence[str]) -> list[Proposal]`, `list_all(conn) -> list[Proposal]`
  - `update_status(conn, pid: int, status: str, *, error: str | None = None, mtime: float | None = None, size: int | None = None, kind: str | None = None, current_tags: dict | None = None) -> None` — `error` follows the arg (None clears; added after Task 2 review so error text never goes stale), `current_tags` is written only when passed (None leaves stored tags alone; Task 4's rescan uses it to refresh tags on a changed file).
  - `set_candidates(conn, pid: int, candidates: list[dict], *, kind: str | None = None) -> None` — sets status `awaiting-review`, clears `error`.
  - `set_beets_item_id(conn, pid: int, item_id: int) -> None`
  - `start_scan(conn, *, kind: str) -> int`, `update_scan(conn, scan_id: int, **fields) -> None`, `latest_scan(conn) -> dict | None` — scans table: `id, kind, phase, seen, queued, fingerprinted, errors, status, message, started_at, finished_at`.

- [x] **Step 1: Write the failing tests** — `tests/test_db.py`, key cases:

```python
def test_enqueue_creates_pending(tmp_path):
    conn = db.connect(tmp_path / "q.db"); db.init_db(conn)
    p = db.enqueue(conn, path="/m/a.flac", mtime=1.0, size=2, kind="needs-tags",
                   current_tags={"title": None})
    assert p.status == "pending" and p.kind == "needs-tags"

def test_enqueue_same_path_preserves_decision(tmp_path):
    ... enqueue, db.update_status(conn, p.id, "skipped")
    again = db.enqueue(conn, path="/m/a.flac", mtime=9.0, size=9, kind="needs-tags", current_tags={})
    assert again.id == p.id and again.status == "skipped"  # decision sticks; mtime NOT overwritten

def test_set_candidates_moves_to_awaiting_review(tmp_path): ...
def test_update_status_records_error_text(tmp_path): ...
def test_list_by_status_filters(tmp_path): ...
def test_json_roundtrip_of_tags_and_candidates(tmp_path): ...  # dict with nested list survives
def test_scans_roundtrip(tmp_path): ...  # start → update phase/counters → latest_scan returns them
```

- [x] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_db.py -v` → FAIL `ModuleNotFoundError: No module named 'app.db'`.

- [x] **Step 3: Implement `app/models.py` + `app/db.py`**

Schema: `proposals(path TEXT UNIQUE NOT NULL, mtime REAL, size INTEGER, kind TEXT, status TEXT, current_tags TEXT /*JSON*/, candidates TEXT /*JSON*/, error TEXT, beets_item_id INTEGER, id INTEGER PRIMARY KEY AUTOINCREMENT)` and `scans(...)`. JSON columns de/serialized with `json.loads/json.dumps`. `enqueue` uses `INSERT … ON CONFLICT(path) DO NOTHING` followed by a select, so existing rows (and their statuses) win.

- [x] **Step 4: Run to verify pass** — `.venv/bin/pytest tests/test_db.py -v` → all passed.

- [x] **Step 5: Commit** — `git add -A && git commit -m "feat: sqlite proposal queue with sticky decisions"`

---

### Task 3: beets adapter — setup, tag reading, expected path (+ fixtures)

**Files:**
- Create: `tests/fixtures/silence.flac`, `tests/fixtures/make_fixtures.sh`, `app/beets_adapter.py`, `tests/conftest.py`, `tests/test_beets_adapter.py`

**Interfaces:**
- Consumes: `app.config.Config` (Task 1).
- Produces (`app/beets_adapter.py` — the only module importing beets):
  - `DEFAULT_TEMPLATE = "$albumartist/$album/$track - $title"`
  - `ensure_setup(cfg: Config) -> None` — idempotent. First call: write `cfg.data_dir/config.yaml` if absent (keys: `directory`, `library`, `paths.default/singleton/comp` all `DEFAULT_TEMPLATE`); `config.read(user=False, defaults=True)` once per process; load that YAML with `pyyaml` and apply `directory`, `library`, `paths.*` via confuse item assignment; `beets.plugins.load_plugins()`; build the singleton `Library(cfg.data_dir / "beets.db", directory=cfg.music_dir)`.
  - `lib() -> Library` — the singleton (raises if `ensure_setup` not called).
  - `read_fields(path: Path) -> dict` — keys `title, artist, album, albumartist, track, disc, year` from `Item.from_path` (values may be `None`).
  - `expected_path(path: Path) -> Path` — `Item.from_path(path).destination()` (bytes → `Path`), using the configured template and `basedir = lib().directory`.
  - `_reset() -> None` — test-only: clears the singleton and the config-loaded flag.
  - Exceptions defined here for later tasks: `DestinationExists(Exception)`, `FileChanged(Exception)`, `ApplyFailed(Exception)` with optional `.item_id: int | None`.
- Produces (`tests/conftest.py`): `cfg` fixture (tmp `music/` + `data/` dirs → `Config`), `make_track(dir, filename, **tags)` helper copying `tests/fixtures/silence.flac` and setting tags via `mediafile.MediaFile`, and an **autouse** fixture that calls `beets_adapter._reset()` before every test — beets config and the library singleton are process-global, so each test re-runs `ensure_setup` against its own tmp dirs.

- [x] **Step 1: Generate the audio fixture**

```bash
mkdir -p tests/fixtures
ffmpeg -f lavfi -i anullsrc=r=44100:cl=stereo -t 0.2 -c:a flac tests/fixtures/silence.flac -y
cat > tests/fixtures/make_fixtures.sh <<'EOF'
#!/bin/sh
# Regenerates tests/fixtures/silence.flac (0.2s of silence).
ffmpeg -f lavfi -i anullsrc=r=44100:cl=stereo -t 0.2 -c:a flac "$(dirname "$0")/silence.flac" -y
EOF
chmod +x tests/fixtures/make_fixtures.sh
```
Expected: `tests/fixtures/silence.flac` exists and is < 50 KB.

- [x] **Step 2: Write the failing tests** — `tests/test_beets_adapter.py`:

```python
def test_ensure_setup_writes_config_yaml(cfg):
    beets_adapter.ensure_setup(cfg)
    text = (cfg.data_dir / "config.yaml").read_text()
    assert "directory:" in text and beets_adapter.DEFAULT_TEMPLATE in text

def test_ensure_setup_is_idempotent(cfg):
    beets_adapter.ensure_setup(cfg); first = beets_adapter.lib()
    beets_adapter.ensure_setup(cfg)
    assert beets_adapter.lib() is first

def test_read_fields_returns_tag_values(cfg, make_track):
    p = make_track(cfg.music_dir, "t.flac", title="Hello", artist="A", album="B",
                   albumartist="A", track=1, disc=1, year=2001)
    assert beets_adapter.read_fields(p) == {
        "title": "Hello", "artist": "A", "album": "B", "albumartist": "A",
        "track": 1, "disc": 1, "year": 2001}

def test_read_fields_untagged_is_all_none(cfg, make_track):
    p = make_track(cfg.music_dir, "u.flac")
    assert all(v is None for v in beets_adapter.read_fields(p).values())

def test_expected_path_follows_template(cfg, make_track):
    p = make_track(cfg.music_dir, "x.flac", title="Test Title", artist="Test Artist",
                   album="Test Album", albumartist="Test Artist", track=1)
    assert beets_adapter.expected_path(p) == (
        cfg.music_dir / "Test Artist" / "Test Album" / "01 - Test Title.flac")
    # zero-padded track: template's $track renders "01"
```

- [x] **Step 3: Run to verify failure** — `.venv/bin/pytest tests/test_beets_adapter.py -v` → FAIL `ModuleNotFoundError: No module named 'app.beets_adapter'`.

- [x] **Step 4: Implement `app/beets_adapter.py` (setup + reads) and `tests/conftest.py`**

Note on `expected_path`: construct via `Item.from_path`, call `destination()` — beets renders `$track` zero-padded (`01`) and appends the file's extension. `ensure_setup` applies YAML values through confuse assignment (`config["paths"]["default"] = …`) so repeated test re-inits replace rather than stack. `conftest.make_track` sets attributes on `mediafile.MediaFile(path)` then `.save()`.

- [x] **Step 5: Run to verify pass** — `.venv/bin/pytest tests/test_beets_adapter.py -v` → 5 passed. If `$track` padding differs, the test's expected value is authoritative — fix the template/config, not the test.

- [x] **Step 6: Commit** — `git add -A && git commit -m "feat: beets adapter setup, tag reading, expected path"`

---

### Task 4: Scan — classify, enqueue, rescan semantics

**Files:**
- Create: `app/scan.py`, `tests/test_scan.py`

**Interfaces:**
- Consumes: `db.enqueue/get/list_all/update_status`, `beets_adapter.read_fields/expected_path/ensure_setup`, `cfg`.
- Produces:
  - `AUDIO_EXTS: set[str]` — `.mp3 .flac .m4a .mp4 .ogg .opus .wav .wma .aiff .aif .ape .wv`
  - `missing_core(fields: dict) -> bool` — true when any of `artist`, `album`, `title` is `None`/blank, or `track` is `None`/`0`.
  - `classify(fields: dict, actual: Path, expected: Path | None) -> str | None` — `"needs-tags"` if `missing_core`; else `"organize-only"` if `expected != actual`; else `None`.
  - `walk(music_dir: Path) -> Iterator[Path]` — audio files only, skips dot-directories.
  - `run(cfg: Config) -> dict` — full scan pass; returns stats `{"seen", "queued", "errors", "missing_files"}` and updates the `scans` row. Rescan rules: no row → classify + enqueue; row exists → untouched unless status `done` and `(mtime, size)` changed (recompute kind; if now `None` keep `done` with updated mtime/size, else reset to `pending` with new kind/tags/mtime/size); open rows (`pending`/`awaiting-review`) whose file vanished → status `error`, message `file no longer exists`. Read exceptions per file → counted in `errors`, no proposal.

- [x] **Step 1: Write the failing tests** — `tests/test_scan.py`:

```python
def _seed_tree(cfg, make_track):
    make_track(cfg.music_dir, "untagged.flac")                                   # no tags
    make_track(cfg.music_dir, "Artist/Album/01 - Song.flac",
               title="Song", artist="Artist", album="Album", albumartist="Artist", track=1)
    make_track(cfg.music_dir, "loose.flac",
               title="Song2", artist="Artist2", album="Album2", albumartist="Artist2", track=2)
    (cfg.music_dir / "notes.txt").write_text("x")

def test_scan_classifies_and_enqueues(cfg, make_track):
    _seed_tree(cfg, make_track)
    stats = scan.run(cfg)
    assert stats["seen"] == 3 and stats["queued"] == 2
    assert db.get_by_path(conn, ".../untagged.flac").kind == "needs-tags"
    assert db.get_by_path(conn, ".../loose.flac").kind == "organize-only"
    assert db.get_by_path(conn, ".../Artist/Album/01 - Song.flac") is None   # complete + in place
    assert db.get_by_path(conn, ".../notes.txt") is None

def test_rescan_preserves_decisions(cfg, make_track): ...
    # enqueue → skip one, error one, leave one pending; re-run scan:
    # skipped stays skipped, error stays error (same message), pending stays pending

def test_rescan_resets_done_when_file_changed(cfg, make_track): ...
    # mark a proposal done, then strip its tags (rewrite file) → re-run →
    # status "pending", kind "needs-tags", mtime updated

def test_rescan_leaves_done_unchanged_alone(cfg, make_track): ...
    # done + untouched (mtime/size same) → still done, no re-enqueue

def test_open_row_missing_file_becomes_error(cfg, make_track): ...
    # pending row, delete file, re-run → status error, "file no longer exists"

def test_scan_does_not_modify_files(cfg, make_track):
    _seed_tree(cfg, make_track)
    before = {p: (p.stat().st_mtime, p.stat().st_size) for p in cfg.music_dir.rglob("*") if p.is_file()}
    scan.run(cfg)
    after = {p: (p.stat().st_mtime, p.stat().st_size) for p in cfg.music_dir.rglob("*") if p.is_file()}
    assert before == after
```

- [x] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_scan.py -v` → FAIL `ModuleNotFoundError: No module named 'app.scan'`.

- [x] **Step 3: Implement `app/scan.py`**

Each file: `fields = beets_adapter.read_fields(p)`; on exception → `stats["errors"] += 1`, continue. `expected = None if missing_core(fields) else beets_adapter.expected_path(p)`; `kind = classify(fields, p, expected)`. Upsert flow per rescan rules above. `ensure_setup(cfg)` at entry.

- [x] **Step 4: Run to verify pass** — `.venv/bin/pytest tests/test_scan.py -v` → all passed.

- [x] **Step 5: Commit** — `git add -A && git commit -m "feat: library scan with sticky rescan semantics"`

---

### Task 5: Fingerprinting and candidate generation

**Files:**
- Create: `app/fingerprint.py`, `tests/test_fingerprint.py`
- Modify: `app/beets_adapter.py` (add `candidates_for`), `tests/test_beets_adapter.py` (add candidate tests)

**Interfaces:**
- Consumes: pyacoustid (`import acoustid`), beets adapter internals.
- Produces (`app/fingerprint.py`, no beets imports):
  - `class AcoustidFailure(Exception)`
  - `recording_ids(path: Path, app_key: str) -> list[str]` — `acoustid.fingerprint_file(str(path), force_fpcalc=True)` → `(duration, fp_bytes)`; `acoustid.lookup(app_key, fp.decode(), duration, meta="recordings releases", timeout=10)`; mirror chroma's rules: `status == "ok"`, top result `score >= 0.5`, take up to 5 recording IDs from its `recordings`. Any exception → `AcoustidFailure` (message includes file path).
- Produces (`app/beets_adapter.py`):
  - `CANDIDATE_FIELDS = ("title", "artist", "album", "albumartist", "track", "disc", "year", "mb_trackid", "mb_albumid")`
  - `candidates_for(path: Path, ids: list[str]) -> list[dict]` — `item = Item.from_path(path)`; `tag_item(Source.from_item(item), search_ids=ids)` when `ids`, else text-only `tag_item(Source.from_item(item))`; if ID search yields nothing, retry text-only once. Flatten each `TrackMatch`: `{field: match.info.item_data.get(field) for field in CANDIDATE_FIELDS}` plus `"score": round(1 - match.distance.distance, 3)` and `"recommendation": proposal.recommendation.name` on every candidate. Returns `[]` when nothing matches.
  - `default_acoustid_key() -> str` — returns `beetsplug.chroma.API_KEY`.

- [x] **Step 1: Write the failing tests**

`tests/test_fingerprint.py` (monkeypatch `app.fingerprint.acoustid.fingerprint_file` / `.lookup`):

```python
def test_recording_ids_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(fp.acoustid, "fingerprint_file", lambda *a, **k: (1.5, b"abc"))
    monkeypatch.setattr(fp.acoustid, "lookup", lambda *a, **k: {
        "status": "ok", "results": [{"score": 0.9, "recordings": [
            {"id": "r1"}, {"id": "r2"}, {"id": "r3"}, {"id": "r4"},
            {"id": "r5"}, {"id": "r6"}]}]})
    assert fp.recording_ids(Path("/x.flac"), "key") == ["r1", "r2", "r3", "r4", "r5"]  # capped at 5

def test_low_score_returns_empty(...):  # top score 0.4 → []
def test_no_results_returns_empty(...):  # {"status": "ok"} without results → []
def test_lookup_exception_wrapped(...):  # lookup raises RuntimeError → AcoustidFailure
```

`tests/test_beets_adapter.py` additions (monkeypatch `app.beets_adapter.tag_item`):

```python
def test_candidates_for_flattens_track_match(cfg, make_track, monkeypatch):
    p = make_track(cfg.music_dir, "c.flac")
    match = SimpleNamespace(
        info=SimpleNamespace(item_data={"title": "T", "artist": "A", "album": "B",
                                        "albumartist": "A", "track": 3, "disc": 1,
                                        "year": 1999, "mb_trackid": "mt",
                                        "mb_albumid": "ma"}),
        distance=SimpleNamespace(distance=0.06))
    proposal = SimpleNamespace(candidates=[match],
                               recommendation=beets_autotag.Recommendation.strong)
    monkeypatch.setattr(beets_adapter, "tag_item", lambda *a, **k: proposal)
    out = beets_adapter.candidates_for(p, ["rid"])
    assert out == [{**{f: match.info.item_data[f] for f in beets_adapter.CANDIDATE_FIELDS},
                    "score": 0.94, "recommendation": "strong"}]

def test_candidates_for_falls_back_to_text_search(cfg, make_track, monkeypatch):
    # first tag_item call (search_ids given) returns empty, second (no ids) returns match → one candidate
    # assert mock call #2 has search_ids absent/None

def test_candidates_for_empty_when_no_match(...):  # both calls empty → []
def test_real_trackinfo_item_data_has_candidate_fields(...):
    # contract against pinned beets: TrackInfo(title=..., index=3, track_id="mt", medium=1,
    # year=1999, artist=..., album=..., albumartist=...) .item_data contains every CANDIDATE_FIELD key
```

- [x] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_fingerprint.py tests/test_beets_adapter.py -v` → FAIL `ModuleNotFoundError: No module named 'app.fingerprint'`.

- [x] **Step 3: Implement `app/fingerprint.py` and adapter `candidates_for`**

- [x] **Step 4: Run to verify pass** — same command → all passed.

- [x] **Step 5: Commit** — `git add -A && git commit -m "feat: acoustid fingerprinting and beets candidate generation"`

---

### Task 6: Apply — collision-safe write and organize

**Files:**
- Modify: `app/beets_adapter.py`
- Modify: `tests/test_beets_adapter.py`

**Interfaces:**
- Consumes: `lib()`, `Item`, exceptions from Task 3.
- Produces: `apply_approved(path: Path, fields: dict | None, *, move_only: bool, expect: tuple[float, int] | None, beets_item_id: int | None) -> tuple[Path, int | None]`
  Sequence (order is load-bearing):
  1. If `expect` given: `path.stat()`; `(st.st_mtime, st.st_size) != expect` → `FileChanged` (no writes). Missing file → `ApplyFailed("file no longer exists")`.
  2. `item = Item.from_path(path)`; apply `fields` in-memory first (`item.update(fields)`) when not `move_only` — **then** attach `item._db = lib()` and set a truthy `item.id = 1` (verified against beets 2.14.1: `destination()` always reads `self.db.replacements`, and the `Model.db` property runs `_check_db(need_id=True)` — the basedir/path_formats overrides do not avoid it; `lib().add()` reassigns the id on insert) and compute `dest = item.destination(basedir=lib().directory, path_formats=get_path_formats(config["paths"]))` **before any library insert**. (Corrected during Task 6 review: computing `dest` from the on-disk tags checked the *old* path for collisions while `Item.move()` re-renders the *new* path internally — a silent overwrite hole. Fields must be applied in-memory before the collision check; this touches no disk and no database.)
  3. Collision: `dest != path and dest.exists()` → `DestinationExists` (nothing written, no library row, source untouched).
  4. Library sync: if `beets_item_id` resolves via `lib().get_item()` to an item whose `path == path` → reuse it (re-apply `fields` in the next step); else rebuild the item from path with `fields` in-memory and `lib().add(item)`. The id in use is attached to any later exception as `.item_id`.
  5. If not `move_only` and `fields`: `item.update(fields)` (idempotent; ensures the reused-row path has them too); `item.try_write()` returns falsy → `ApplyFailed("failed to write tags", item_id=…)`.
  6. `if dest != path: item.move()` — `OSError` → `ApplyFailed("failed to move file", item_id=…)`.
  7. Return `(dest, item_id)`.

- [x] **Step 1: Write the failing tests** (real beets, tmp library; read tags back with `mediafile.MediaFile`):

```python
def test_happy_path_writes_tags_and_moves(cfg, make_track):
    p = make_track(cfg.music_dir, "old.flac", title="Old")
    dest, item_id = beets_adapter.apply_approved(
        p, {"title": "New", "artist": "A", "album": "B", "albumartist": "A",
            "track": 1, "disc": 1, "year": 2000},
        move_only=False, expect=None, beets_item_id=None)
    assert dest == cfg.music_dir / "A" / "B" / "01 - New.flac" and dest.exists()
    assert mediafile.MediaFile(str(dest)).title == "New"
    assert len(list(beets_adapter.lib().items())) == 1

def test_collision_destination_never_overwritten(cfg, make_track):
    p = make_track(cfg.music_dir, "a.flac", title="T", artist="A", album="B",
                   albumartist="A", track=1)
    dest = cfg.music_dir / "A" / "B" / "01 - T.flac"
    dest.parent.mkdir(parents=True); dest.write_bytes(b"EXISTING")
    with pytest.raises(beets_adapter.DestinationExists):
        beets_adapter.apply_approved(p, {"title": "T"}, move_only=False,
                                     expect=None, beets_item_id=None)
    assert dest.read_bytes() == b"EXISTING"                      # untouched
    assert mediafile.MediaFile(str(p)).title == "T"             # source untouched
    assert list(beets_adapter.lib().items()) == []               # no row inserted

def test_file_changed_rejected(cfg, make_track):
    p = make_track(cfg.music_dir, "c.flac", title="T")
    with pytest.raises(beets_adapter.FileChanged):
        beets_adapter.apply_approved(p, {"title": "X"}, move_only=False,
                                     expect=(1.0, 2), beets_item_id=None)
    assert mediafile.MediaFile(str(p)).title == "T"   # rejected before any write

def test_write_failure_keeps_original_and_retry_succeeds(cfg, make_track, monkeypatch):
    p = make_track(cfg.music_dir, "w.flac")
    monkeypatch.setattr(beets_adapter.Item, "try_write", lambda self, *a, **k: False)
    with pytest.raises(beets_adapter.ApplyFailed) as exc:
        beets_adapter.apply_approved(p, {"title": "New"}, move_only=False,
                                     expect=None, beets_item_id=None)
    assert exc.value.item_id is not None
    assert p.exists() and mediafile.MediaFile(str(p)).title is None   # original path, original tags
    monkeypatch.undo()
    dest, item_id = beets_adapter.apply_approved(p, {"title": "New"}, move_only=False,
                                                 expect=None, beets_item_id=exc.value.item_id)
    assert dest.exists() and len(list(beets_adapter.lib().items())) == 1  # single row

def test_move_failure_retry_single_row(cfg, make_track, monkeypatch):
    # Item.move raises OSError on first call → ApplyFailed(item_id), file still at
    # original path WITH new tags (write already happened) → retry with same id
    # succeeds, exactly one library row, no duplicate.

def test_move_only_leaves_tags_untouched(cfg, make_track):
    p = make_track(cfg.music_dir, "m.flac", title="Keep", artist="A", album="B",
                   albumartist="A", track=1)
    dest, _ = beets_adapter.apply_approved(p, None, move_only=True,
                                           expect=None, beets_item_id=None)
    assert dest.exists() and mediafile.MediaFile(str(dest)).title == "Keep"

def test_expect_match_proceeds(cfg, make_track): ...  # expect == actual stat → success
```

- [x] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_beets_adapter.py -v` → FAIL (`apply_approved` not defined).

- [x] **Step 3: Implement `apply_approved`** exactly in the sequence above.

- [x] **Step 4: Run to verify pass** — `.venv/bin/pytest tests/test_beets_adapter.py -v` → all passed.

- [x] **Step 5: Commit** — `git add -A && git commit -m "feat: collision-safe apply that writes then moves"`

---

### Task 7: Worker — background jobs and progress

**Files:**
- Create: `app/worker.py`, `tests/test_worker.py`

**Interfaces:**
- Consumes: `scan.run`, `fingerprint.recording_ids`, `beets_adapter.candidates_for/default_acoustid_key/ensure_setup`, `db.*`.
- Produces:
  - `run_fingerprint_sync(cfg: Config) -> dict` — processes every `pending` proposal: `recording_ids` + `candidates_for` → `db.set_candidates` (kind stays, except no-candidates → kind `unidentified`, unless current kind is `manual` which is kept); `AcoustidFailure` → status `error` with the message, loop continues. Returns progress dict; increments the scan row's `fingerprinted`/`errors`.
  - `run_scan_sync(cfg: Config) -> dict` — `scan.run` then `run_fingerprint_sync`; persists a `scans` row (`phase` moves `scanning` → `fingerprinting` → `done`); returns final status dict.
  - `start_job(cfg: Config, kind: str = "scan") -> bool` — single worker: `Lock.acquire(blocking=False)`; already held → return `False`. Spawns a **daemon** thread running `run_scan_sync` (`kind="scan"`) or `run_fingerprint_sync` (`kind="fingerprint"`). Always releases the lock in `finally`, including when thread creation/start itself fails (lock released, exception re-raised — verified in Task 7 review). An uncaught job crash is reported through `status()`: `phase` becomes `error` with the exception in `message`. Fingerprint-only jobs are what retry/reset/identify/open-path trigger — they must not re-walk the library.
  - `is_running() -> bool`, `status() -> dict` — shape: `{"running": bool, "phase": str, "seen": int, "queued": int, "fingerprinted": int, "errors": int, "message": str | None, "last_scan": dict | None}` (last_scan from `db.latest_scan`).
- Import style (tests monkeypatch these exact names): `from .fingerprint import recording_ids, AcoustidFailure` and `from .beets_adapter import candidates_for, default_acoustid_key` **into `worker`'s namespace**. AcoustID key resolution: `cfg.acoustid_app_key or default_acoustid_key()`, passed to `recording_ids`.

- [x] **Step 1: Write the failing tests** — `tests/test_worker.py` (fingerprint layer monkeypatched: `worker.recording_ids` and `worker.candidates_for`):

```python
def test_scan_sync_produces_reviewable_proposals(cfg, make_track, monkeypatch):
    # untagged fixture in tree; recording_ids → ["rid"], candidates_for → [{"title": "T", ...}]
    out = worker.run_scan_sync(cfg)
    p = db.get_by_path(conn, ".../untagged.flac")
    assert p.status == "awaiting-review" and p.candidates[0]["title"] == "T"
    assert out["fingerprinted"] == 1

def test_no_candidates_marks_unidentified(...):        # candidates_for → [] → kind "unidentified", awaiting-review
def test_manual_kind_survives_no_candidates(...):     # kind "manual" + [] → stays "manual"
def test_fingerprint_error_parks_proposal_and_continues(...):
    # recording_ids raises AcoustidFailure for the FIRST file, succeeds for second:
    # first → status error with message, second → awaiting-review (loop did not crash)
def test_start_job_rejects_when_busy(cfg, monkeypatch):
    assert worker._lock.acquire(blocking=False)
    try: assert worker.start_job(cfg) is False
    finally: worker._lock.release()
def test_pending_rows_are_resumed(...):
    # manually insert a pending row (as if a previous run died), run_fingerprint_sync → processed
def test_status_shape(...):  # assert exact key set of status()
```

- [x] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_worker.py -v` → FAIL `ModuleNotFoundError: No module named 'app.worker'`.

- [x] **Step 3: Implement `app/worker.py`**

DB connections opened inside each function (`db.connect(cfg.db_path)`), never shared across threads. Module-level `threading.Lock`.

- [x] **Step 4: Run to verify pass** — `.venv/bin/pytest tests/test_worker.py -v` → all passed.

- [x] **Step 5: Commit** — `git add -A && git commit -m "feat: background scan/fingerprint worker with progress"`

---

### Task 8: Web — queue page, scan control, status, open-by-path

**Files:**
- Modify: `app/__init__.py`, create `app/web.py`, `app/templates/base.html`, `app/templates/queue.html`, `app/static/style.css`, `app/static/app.js`, `tests/test_web.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `create_app(cfg: Config | None = None) -> Flask` — loads config (default `load_config()`), `db.init_db`, `beets_adapter.ensure_setup`, registers blueprint, `secret_key = secrets.token_hex(16)` (fresh per start; no env needed).
- Routes (all POSTs are form-encoded, redirect + flash):
  - `GET /` — queue grouped by `group_key(proposal)` = `albumartist | album` from candidate[0] when present, else `current_tags`, else `(no album)`. Shows proposals with status in `pending|awaiting-review|error` by default; `?status=skipped|done` filters to closed ones. Badges: `needs tags`, `organize only`, `unidentified`, `manual`, `error`, `destination exists` (error text starts with `destination exists`), plus `pending` shown as `fingerprinting…`.
  - `GET /api/status` — JSON from `worker.status()`.
  - `POST /scan` — `worker.start_job(cfg)`; busy → flash "scan already running"; else flash "scan started".
  - `POST /open-path` — form field `path`: must resolve inside `cfg.music_dir`, exist, and have an audio ext (else flash error, redirect back). Enqueue: existing row → reset to `pending`, kind `manual`; new row → kind `manual`, status `pending`, current_tags from `read_fields`. Then `worker.start_job(cfg, "fingerprint")` — busy → flash "queued; will be identified by the running job". Redirect to `/proposal/<id>`.
  - `app/static/app.js` — polls `/api/status` every 2 s while `running`, updates the progress line, stops when idle.

- [x] **Step 1: Write the failing tests** — `tests/test_web.py`:

```python
def test_queue_lists_proposals_with_badges(cfg, make_track, client_with_seeded_queue): ...
    # seed needs-tags + unidentified + error("destination exists: …") rows
    # GET / → each path visible; badges "needs tags", "unidentified", "destination exists" visible

def test_queue_groups_by_album(...):          # two tracks same artist/album → one group header
def test_queue_status_filter(...):            # ?status=skipped shows only skipped
def test_status_endpoint_shape(...):          # GET /api/status → exact key set from worker.status
def test_scan_post_starts_job(cfg, client, monkeypatch):
    monkeypatch.setattr(worker, "start_job", lambda c: True)
    resp = client.post("/scan", follow_redirects=True)
    assert resp.status_code == 200 and b"scan started" in resp.data

def test_open_path_valid_file(...):           # fixture inside music_dir → 302 → /proposal/<id>, row pending+manual
def test_open_path_outside_library_rejected(...):  # "/etc/passwd" → flash error, no row
def test_open_path_non_audio_rejected(...):   # ".txt" → flash error
def test_static_assets_served(...):           # GET /static/app.js → 200
```

- [x] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_web.py -v` → FAIL (`create_app` not importable / no routes).

- [x] **Step 3: Implement `create_app`, `app/web.py`, templates**

`client_with_seeded_queue` fixture in `tests/conftest.py`: builds app with tmp cfg, seeds rows via `db.*`. Jinja `group_key` passed pre-computed from a small helper in `web.py` (keep templates dumb).

- [x] **Step 4: Run to verify pass** — `.venv/bin/pytest tests/test_web.py -v` → all passed.

- [x] **Step 5: Commit** — `git add -A && git commit -m "feat: queue page with scan control and status polling"`

---

### Task 9: Web — proposal detail and review actions

**Files:**
- Modify: `app/web.py`, `app/templates/proposal.html`, `tests/test_web.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: `beets_adapter.apply_approved` + exceptions, `db.*`, `worker.start_job`.
- Produces (routes):
  - `GET /proposal/<int:pid>` — side-by-side current vs. proposed, candidate list with `score` as percent + `recommendation`, edit form pre-filled from candidate[0] (or current tags when none), buttons: **Approve** (radio `candidate`, only when candidates exist), **Save & approve edits** (all text fields; `track/disc/year` coerced `int`, blank → `None`; only `CANDIDATE_FIELDS` subset submitted), **Move only** (kind `organize-only`), **Skip**, **Identify again**, **Retry** (status `error`), **Reset to queue** (status `skipped`).
  - `POST /proposal/<int:pid>/approve` — `action=candidate|edits|move`:
    - Re-read proposal; **mtime/size check** (`expect=(proposal.mtime, proposal.size)`) applies **only when `proposal.status == "awaiting-review"`**; `error` status re-approvals pass `expect=None` (our own earlier write may have changed the stat).
    - `action=candidate` → fields = candidate dict restricted to `CANDIDATE_FIELDS`; `action=edits` → fields from form; `action=move` → `move_only=True, fields=None` (requires kind `organize-only`).
    - `kind=unidentified` with `action=candidate` → 400/flash (must edit).
    - Success → status `done`, `set_beets_item_id`, flash "tagged and moved to <dest>". `DestinationExists`/`FileChanged`/`ApplyFailed` → status `error` with `str(exc)` (store `exc.item_id` when present), flash the message. Redirect to queue.
  - `POST /proposal/<int:pid>/skip` → `skipped`.
  - `POST /proposal/<int:pid>/reset` → candidates non-empty → `awaiting-review`; else `pending` + `start_job(cfg, "fingerprint")`.
  - `POST /proposal/<int:pid>/retry` → same reopen logic as `reset` (shared helper `_reopen`).
  - `POST /proposal/<int:pid>/identify` → clear candidates, status `pending`, `start_job(cfg, "fingerprint")`.
  - `POST /albums/approve` → form field `album_key`: every `awaiting-review` proposal in that group — with candidates → approve candidate[0]; `organize-only` → move-only; `unidentified`/`pending` → skipped over. Collect per-file errors (they become `error` rows). Flash `approved N, failed M`. Redirect to queue.

- [x] **Step 1: Write the failing tests** — additions to `tests/test_web.py` (`apply_approved` monkeypatched to `beets_adapter` where the test is about *routing*, real adapter where noted):

```python
def test_approve_candidate_writes_and_marks_done(...):
    # POST action=candidate, candidate=0 → apply_approved called with CANDIDATE_FIELDS
    # subset; status done; redirect; flash "tagged and moved"

def test_approve_edits_coerces_types(...):
    # form track="3", year="" → fields {"track": 3, "year": None}

def test_approve_unidentified_without_fields_rejected(...):
    # kind unidentified + action=candidate → proposal NOT done

def test_approve_with_changed_mtime_errors(...):
    # proposal seeded with stale mtime; real apply_approved (real beets) →
    # status error, message contains "changed"; file untouched

def test_approve_collision_surfaces_badge(...):
    # pre-create destination file; real apply → status error with "destination exists";
    # GET / shows badge "destination exists"

def test_skip_and_reset_roundtrip(...):       # skip → skipped; reset → awaiting-review (had candidates)
def test_retry_without_candidates_requeues(...):  # error + [] → pending + start_job called
def test_identify_clears_candidates(...):     # → pending, candidates == []
def test_bulk_approve_partial_failure(...):
    # album with 3 awaiting-review (2 ok, 1 collision via real apply):
    # → 2 done, 1 error; flash contains "approved 2"; all three rows correct status
def test_bulk_approve_ignores_unidentified(...):  # unidentified row not attempted, not errored
```

- [ ] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_web.py -v` → FAIL (new routes 404).

- [x] **Step 3: Implement routes + `proposal.html`**

- [x] **Step 4: Run to verify pass** — `.venv/bin/pytest tests/test_web.py -v` → all passed.

- [x] **Step 5: Commit** — `git add -A && git commit -m "feat: review actions — approve, edit, skip, retry, bulk"`

---

### Task 10: Settings page and UI polish

**Files:**
- Modify: `app/web.py`, `app/templates/settings.html`, `app/templates/base.html`, `app/static/style.css`, `app/static/app.js`, `tests/test_web.py`

**Interfaces:**
- Produces: `GET /settings` — read-only: `music_dir`, `data_dir`, effective `paths.default` template, AcoustID key source (`custom` when `cfg.acoustid_app_key` set, else `beets default` — presence-based, no network validity check in v1), proposal counts per status, last scan (`db.latest_scan`).

- [x] **Step 1: Write the failing test**

```python
def test_settings_page_shows_configuration(cfg, client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert str(cfg.music_dir).encode() in resp.data
    assert b"$albumartist/$album/$track - $title" in resp.data
    assert b"beets default" in resp.data          # no ACOUSTID_APP_KEY in test env
```

- [x] **Step 2: Run to verify failure** — `.venv/bin/pytest tests/test_web.py::test_settings_page_shows_configuration -v` → FAIL (404).

- [x] **Step 3: Implement `/settings` + polish**

Finish `style.css` (readable table-ish queue, visible focus states, badges with distinct colors, dark-mode-friendly via `prefers-color-scheme`), `base.html` nav (Queue · Settings), and `app.js` progress line. Plain semantic HTML — no framework, no build step.

- [x] **Step 4: Run full suite** — `.venv/bin/pytest -v` → all passed.

- [x] **Step 5: Commit** — `git add -A && git commit -m "feat: settings page and ui polish"`

---

### Task 11: Docker, docs, end-to-end smoke

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `run.py`, `README.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Produces: `run.py` — `cfg = load_config(); app = create_app(cfg); waitress.serve(app, host=cfg.host, port=cfg.port)`.

- [ ] **Step 1: Write `run.py`, `Dockerfile`, `docker-compose.yml`, `.dockerignore`**

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromaprint-tools ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
ENV MUSIC_DIR=/music DATA_DIR=/data
CMD ["python", "run.py"]
```

```yaml
# docker-compose.yml
services:
  taggerz:
    build: .
    ports: ["127.0.0.1:8080:8080"]
    volumes:
      - ./music:/music
      - taggerz-data:/data
    environment:
      ACOUSTID_APP_KEY: "${ACOUSTID_APP_KEY:-}"
volumes:
  taggerz-data:
```

`.dockerignore`: `.venv`, `tests`, `docs`, `music`, `__pycache__`, `.git`.
(Note: `chromaprint-tools` provides `fpcalc`; `ffmpeg` supplies decode support per spec. Root user in container is an explicit v1 call — personal localhost tool; README documents it.)

- [ ] **Step 2: Build the image**

Run: `docker build -t taggerz .`
Expected: completes without error; `docker run --rm taggerz python -c "import acoustid, flask, waitress; import beets; print(beets.__version__)"` → `2.14.1`.

- [ ] **Step 3: End-to-end smoke test against a sample tree**

```bash
docker compose down -v          # fresh state for a deterministic smoke run
mkdir -p music && cp tests/fixtures/silence.flac music/untagged.flac
docker compose up -d
# wait for health, then:
curl -fsS http://127.0.0.1:8080/ | grep -i queue          # page up
curl -fsS -X POST http://127.0.0.1:8080/scan -L            # start scan (form post)
# poll until a proposal is awaiting-review:
curl -fsS http://127.0.0.1:8080/api/status
# approve with manual edits (silence will not fingerprint to a match):
curl -fsS -X POST http://127.0.0.1:8080/proposal/1/approve -L \
  -d "action=edits" -d "title=Smoke Title" -d "artist=Smoke Artist" \
  -d "album=Smoke Album" -d "albumartist=Smoke Artist" -d "track=1" -d "year=2020"
docker compose exec taggerz python -c "
import mediafile; m = mediafile.MediaFile('/music/Smoke Artist/Smoke Album/01 - Smoke Title.flac')
assert m.title == 'Smoke Title'; print('E2E OK')"
docker compose down -v && rm -rf music
```
Expected: `E2E OK`; file landed at the template path with written tags; queue page and status endpoint worked. (If AcoustID is reachable and returns a match for the file, approve via `action=candidate` instead — either path proves the flow.)

- [ ] **Step 4: Write `README.md`**

Cover: what it does (scan → review → approve → organize), quick start (`docker compose up`, open `http://localhost:8080`), the review flow with a text sketch of the queue, configuration (`MUSIC_DIR`, `DATA_DIR`, `PORT`, optional `ACOUSTID_APP_KEY` — explain the default key and that registration is only needed for future fingerprint submission), editing the path template in `taggerz-data`'s `config.yaml` + restart, safety guarantees (no writes without approve, never overwrites, never deletes), and development commands (`uv` setup, `.venv/bin/pytest`).

- [ ] **Step 5: Update `AGENTS.md` with verified commands**

Replace TBD sections with facts only: project name/purpose, `.venv/bin/pytest` (tests), `docker build -t taggerz .`, `docker compose up`, fixture regeneration script, and gotchas (beets pinned at 2.14.1; beets imports only in `app/beets_adapter.py`; fixtures need ffmpeg only to regenerate).

- [ ] **Step 6: Full verification**

Run: `.venv/bin/pytest -v`
Expected: all passed.
Run: `docker compose up -d --build && curl -fsS http://127.0.0.1:8080/ >/dev/null && docker compose down -v`
Expected: clean start and shutdown.

- [ ] **Step 7: Commit** — `git add -A && git commit -m "feat: docker packaging, readme, and e2e smoke"`
