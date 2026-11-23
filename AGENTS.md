# AGENTS.md

## Project

- Name: taggerz — self-hosted Flask tool that scans a music library for missing metadata, proposes tags via Chromaprint/AcoustID + beets, lets the user review/approve in a web UI, then writes tags and organizes approved files.
- Stack: Python 3.12, Flask, waitress, beets==2.14.1, Chromaprint (fpcalc), SQLite, Docker.
- Entry points: `run.py` (waitress) → `app.create_app`. All beets imports live only in `app/beets_adapter.py`.

## Commands

- Tests: `.venv/bin/pytest -v` (87 tests).
- Docker build: `docker build -t taggerz .`
- Docker run (smoke): `docker compose up -d --build && curl -fsS http://127.0.0.1:8080/ >/dev/null && docker compose down -v`
- Fixture regen: `tests/fixtures/make_fixtures.sh` (requires ffmpeg).
- Toolchain: `uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt`.

## Architecture

- `app/config.py` — env-driven frozen Config (MUSIC_DIR, DATA_DIR, PORT, HOST, ACOUSTID_APP_KEY).
- `app/models.py` / `app/db.py` — SQLite proposal queue (sticky decisions).
- `app/beets_adapter.py` — sole beets importer: setup, read_fields, expected_path, candidates_for, apply_approved.
- `app/scan.py` — walk + classify + rescan semantics.
- `app/fingerprint.py` — pyacoustid only (no beets).
- `app/worker.py` — background scan/fingerprint jobs with progress.
- `app/web.py` + templates — queue, proposal detail, settings, review actions.

## Conventions & Gotchas

- beets pinned exactly at 2.14.1; do not upgrade without re-running adapter contract tests.
- All `import beets` / `import beetsplug` only in `app/beets_adapter.py` (tests may import beets freely).
- `ffmpeg` is only needed to regenerate the test fixture; the container installs it for decode support.
- Docker base image package for fpcalc is `libchromaprint-tools` (not `chromaprint-tools` on Debian trixie).
- Network (AcoustID/MusicBrainz) is always mocked in pytest; tests pass offline.
- `.dockerignore` excludes tests/docs/music/.venv/.git.
- Changing the compose `user:` directive after the `taggerz-data` volume exists requires chowning the volume to that uid/gid (see README Troubleshooting); `create_app` probes `/data` writability at boot and fails fast with the instruction.
