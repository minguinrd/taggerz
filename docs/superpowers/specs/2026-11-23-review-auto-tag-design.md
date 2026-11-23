# taggerz — Review & Auto-Tag Music Library: Design

**Date:** 2026-11-23
**Status:** draft, awaiting human approval
**Stack (fixed by user):** Python Flask · Docker · beets · Chromaprint

## Intent

A personal, self-hosted web tool that scans a music library folder for files
with missing metadata, proposes tags using acoustic fingerprinting
(Chromaprint → AcoustID → MusicBrainz, via beets' chroma-enabled autotagger),
lets the user review every proposal in a browser, and only then writes tags and
organizes approved files into a proper folder structure.

The user's why: their library has files with missing/wrong tags and no
structure. They want it fixed, but they want to **see and approve** every
change before it touches their files. Nothing gets written without an explicit
approve.

## Goals

1. Scan a mounted library folder (background job, resumable) for audio files
   that (a) are missing core metadata (artist, album, title, or track number)
   or (b) sit at a path that doesn't match the configured organize template.
2. Fingerprint files that need identification (`fpcalc` via pyacoustid) and
   look up AcoustID → MusicBrainz candidates through beets' autotagger
   (`beets[chroma]`).
3. Persist a review queue (SQLite) that survives restarts.
4. Web UI to review each proposal: current vs. proposed tags, candidate list
   with match scores, per-file **Approve / Edit / Skip**, and **approve-all
   for an album**.
5. On approve: write tags and move the file into the organize template —
   executed by beets, not by hand-rolled write code.
6. Files AcoustID can't identify get an "unidentified" badge in the same
   queue: user may enter tags manually or skip. They never block the scan.
7. Escape hatch: an "identify anyway" action on any queued file, plus a
   small "open file by path" input so files the scan skipped (complete but
   wrong tags) can be pulled into the queue by typing their path.
8. Visible progress: scan/fingerprint status, counts, per-file errors.
9. One-command start: `docker compose up`.

## Non-goals (v1)

- Cover art fetching/embedding (beets `fetchart` is a natural later addition).
- Auth/multi-user — localhost personal tool.
- Playing audio, playlists, streaming, lyrics.
- Deduplication detection.
- A general-purpose beets UI (only the scan → review → tag → organize flow).
- Editing files outside the mounted library.
- Automatic/scheduled scans (scan is user-triggered; cron can come later).

## Anti-goals

- **No writes to audio files without explicit approval in the UI.** Scanning
  and fingerprinting are read-only with respect to the library; only the
  approve action mutates files.
- **Never delete files.** Moves only, and only on approve.
- **Never overwrite.** If an approve-time destination already exists (or two
  proposals target the same path), the proposal fails with a visible
  "destination already exists" badge and stays open — resolving the
  collision is the user's call, done outside the tool.
- **Never silently reorganize** files that are already tagged and already at a
  matching path.
- **No silent failures:** a failed write/move is surfaced in the UI with the
  error, the proposal stays open.

## Constraints

- Stack is fixed: Flask, Docker, beets, Chromaprint.
- Library arrives as a read-write Docker volume; tree may be large
  (10k+ files) — scanning must not block the web UI.
- AcoustID lookups use an *application* API key: the default is beets'
  embedded key (verified in chroma source — registration is only needed
  for fingerprint *submission*, which is out of scope). `ACOUSTID_APP_KEY`
  env var optionally overrides it.
- Fingerprinting is CPU-heavy — worker runs sequentially (or low
  concurrency), reports progress, and is resumable across restarts.
- beets' Python internals are not a stable public API — pin the beets
  version and isolate every beets import behind one adapter module.
- Target environment: Linux host with Docker (verify at build time).

## Approaches considered

| # | Approach | Trade-off | Decision |
|---|----------|-----------|----------|
| 1 | **Flask drives beets in-process**: Flask scans/queues/reviews; beets' autotag generates candidates and applies approved changes; beets' path templates do the organizing. | Maximum reuse (matching, scoring, organizing, beets library DB). Depends on beets internals → mitigated by version pin + adapter isolation. | **Chosen.** |
| 2 | Flask does everything itself (pyacoustid + direct MusicBrainz API + mutagen); beets unused. | Full control, one simple process — but reimplements candidate diffing/scoring/organizing that beets already does well. More code, more bugs; "using beets" becomes decorative. | Rejected. |
| 3 | beets plugin publishes candidates via an API; Flask is only the front-end. | beets stays usable from terminal alongside the UI, but two services + a queue protocol — most moving parts for a personal tool. | Rejected. |

## Architecture (chosen approach)

Single Docker container, one Python process:

```
┌──────────────────────────────────────────────────────────┐
│ Flask app (waitress)                                     │
│                                                          │
│  scan worker      fingerprint worker     beets_adapter   │
│  (walk tree,      (fpcalc → AcoustID     (candidate gen  │
│   find gaps)       → beets candidates)    + apply/move)  │
│       │                 │                    │           │
│       └────────────┬────┴────────────────────┘           │
│                    ▼                                     │
│            SQLite queue (proposals, scans, results)      │
│                    ▲                                     │
│  web routes + Jinja templates (review UI)                │
└──────────────────────────────────────────────────────────┘
     │ /music (rw volume)        │ /data (volume)
     ▼                           ▼
  audio files              queue.db · beets config + beets.db · logs
```

### Components

- **`scan`** — walks `MUSIC_DIR` in a background thread. For each audio file:
  read tags (via beets/mutagen). Complete tags **and** path matching the
  template → skip (counted in stats). Otherwise enqueue a proposal.
  Read-only against the library.
- **`fingerprint`** — worker over pending proposals: pyacoustid + `fpcalc` →
  AcoustID lookup → beets autotag candidate matching (`tag_item` by
  recording ID, text-search fallback). Stores top candidates with scores
  on the proposal. Resumable by status: a proposal leaves `pending` once
  processed, so a restarted job never redoes it.
- **`beets_adapter`** — the *only* module that imports beets. Two jobs:
  1. **Generate candidates** for a file (beets autotag matching, chroma
     enabled).
  2. **Apply an approved proposal** — write tags and move the file per the
     beets path template; register/update it in the beets library DB.
     Also handles "organize-only" proposals (move, no tag changes) and
     manual-edit proposals (write user-supplied fields, then organize).
- **`queue`** — SQLite. Tables (sketch): `scans`, `proposals` (UNIQUE path —
  one row per file, statuses persist across scans,
  mtime/size snapshot, type: `needs-tags` | `organize-only` |
  `unidentified` | `manual`, status: `pending` | `awaiting-review` |
  `approved` | `skipped` | `error`, current tags JSON, candidates JSON,
  decided_at, error text), `settings`.
- **`web`** — server-rendered Jinja + minimal vanilla JS. Pages:
  - `/` — queue grouped by album with badges (`needs tags`, `organize only`,
    `unidentified`, `error`, `destination exists`), scan button + live
    progress, bulk approve-per-album, and a small "open file by path" input
    that pulls a skipped file into the queue.
  - `/proposal/<id>` — side-by-side current vs. proposed tags, candidate
    list with scores, edit form, Approve / Edit / Skip / Identify-anyway.
  - `/settings` — read-only status: AcoustID key validity, last scan,
    counts, path template in effect.

### Flow

1. User clicks **Scan** → background walk begins; progress shown on `/`.
2. Each gap file becomes a proposal; files needing identification go through
   fingerprint → candidates.
3. User reviews in the queue: approve the default candidate, pick another,
   edit fields by hand, or skip.
4. Approve → destination pre-check (collision → visible error, no write) →
   `beets_adapter` writes + moves; proposal marked `done` with the result;
   failures become `error` with the message.
5. **Rescan semantics:** proposals are keyed uniquely by path, so decisions
   persist across scans. Rescan only adds *new* files. Already-`done` files
   are skipped when unchanged (mtime + size match). `skipped` stays skipped
   (with a per-proposal "reset to queue" action); `error` stays parked with
   a Retry button — nothing is auto-retried or re-asked without the user.
   Anything changed on disk since enqueueing is re-checked right before
   write — if it changed under us, the proposal errors rather than
   overwriting.

## Docker

- **`Dockerfile`**: `python:3.12-slim`; apt install `chromaprint-tools`
  (provides `fpcalc`) and `ffmpeg` (decode support for fingerprinting);
  pip install `flask`, `beets[chroma]` (pulls pyacoustid), `waitress`.
- **`docker-compose.yml`**: service `taggerz`
  - ports `127.0.0.1:8080:8080`
  - volumes: `./music:/music:rw`, named volume `taggerz-data:/data`
  - env: `ACOUSTID_APP_KEY` (optional override), `MUSIC_DIR=/music`,
    `DATA_DIR=/data`
  - healthcheck on `/` .
- First boot generates a beets config in `/data/config.yaml` if absent:
  `directory: /music`, `library: /data/beets.db`, and `paths` set to the
  organize template (default, singleton, comp). The app drives AcoustID
  lookups directly (chroma's import-pipeline integration isn't used);
  beets' built-in `musicbrainz` plugin — on by default — resolves IDs
  into candidates.

## Decisions & defaults (my calls — flag anything you'd change)

- **Path template:** `$albumartist/$album/$track - $title`, pinned for all
  three beets path-format keys (default, singleton, comp) so every file
  organizes the same way. Stored in beets config; changing it later is a
  config edit, not UI.
- **"Missing metadata" =** any of artist / album / title / track empty →
  `needs-tags` proposal.
- **"Complete tags but wrong"** is out of automatic scope; the manual
  trigger is *Identify anyway* on queued files, plus the "open file by
  path" input for files the scan skipped (user knows the path; no full
  browse/search page in v1).
- **Collisions:** destination pre-check before every write; existing
  destination → proposal errors with `destination exists` badge and stays
  open; never overwrite, never auto-rename (resolution happens outside
  the tool).
- **Rescan:** proposals keyed by path; decisions persist (`skipped` stays
  skipped with a reset action, `error` parked with Retry); rescan adds
  only new files.
- **Organize-only moves:** files with complete tags whose current path
  doesn't match the template get a `organize-only` proposal (move, no tag
  writes).
- **UI:** server-rendered Jinja, no SPA/build step. Vanilla JS only for
  progress polling and bulk actions.
- **Queue DB:** SQLite in `/data`, separate from beets' own `beets.db`
  (both in `/data`; coexists with later `beet` CLI use).
- **Scan trigger:** manual button, background thread — no cron in v1.
- **Audio formats:** everything mutagen supports (mp3, flac, m4a/mp4, ogg,
  opus, wma, aiff…); non-audio files ignored.
- **Server:** waitress (single process, threads play well with the scan
  worker; no forking complexity).
- **No auth**, bind to localhost only.
- **beets version pinned** exactly in `requirements.txt`.
- **Fresh beets setup confirmed** by the user: no existing beets config or
  library on the host. First boot generates a fresh one in `/data`; taggerz
  is the only writer (running `beet` in a terminal alongside it is out of
  scope).
- Repo will be `git init`'d at build start (currently empty, no git).

## Risks

| Risk | Mitigation |
|------|-----------|
| beets internal API drift breaks adapter | Pin exact version; all beets imports in one module; contract tests against the adapter. |
| Low AcoustID match rate on obscure files | Unidentified badge + manual edit path; scan never blocks. |
| Long fingerprint times on big libraries | Sequential worker, progress UI, resumable scan, fingerprint cache. |
| File modified on disk between scan and approve | mtime/size re-check before write; mismatch → error, never overwrite. |
| Duplicate destinations (dupes in library) | Destination pre-check on approve; collision → visible `destination exists` error, never overwrite. |
| Fingerprint CPU load | Worker runs at low concurrency inside the container; user controls when scans run. |

## What's left to the builder

- Implementation order and test strategy (plan phase).
- Exact beets API calls for candidate generation and apply — verify against
  the pinned beets version (adapter is the single place this lives).
- Template rendering, progress polling details, error copy.
- Docker build verification with a sample library (include a tiny fixture
  library in the repo for tests).
- First-boot beets config generation details.
