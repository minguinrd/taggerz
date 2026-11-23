# taggerz

A self-hosted web tool that scans a music library for missing metadata,
proposes tags via Chromaprint/AcoustID + beets, lets you review every
change in a browser, then writes tags and organizes approved files —
never touching a file without approval.

## What it does

1. **Scan** — walks the library, finds audio files missing core metadata
   (artist/album/title/track) or sitting at a path that doesn't match the
   organize template. Read-only.
2. **Fingerprint** — runs Chromaprint (`fpcalc`) and looks up AcoustID →
   MusicBrainz, generating candidate tags via beets.
3. **Review** — queue grouped by album, with match scores, edit forms, skip,
   retry, and bulk approve-per-album.
4. **Approve** — writes tags and moves the file into the template path.
   Collisions and changed files are surfaced as errors, never overwritten.

## Quick start

```bash
docker compose up
```

Open <http://localhost:8080>. Put your music in `./music` (mounted read-write
at `/music`).

## Review flow

```
Scan → proposals (pending) → fingerprint → awaiting-review
  └─ approve candidate / edit & approve / move-only / skip
       └─ done  (or error → retry)
```

## Configuration

Environment variables:

- `MUSIC_DIR` (default `/music`)
- `DATA_DIR` (default `/data`)
- `PORT` (default `8080`)
- `HOST` (default `0.0.0.0`)
- `ACOUSTID_APP_KEY` (optional override)

AcoustID uses beets' embedded application key by default; registration is
only needed for fingerprint *submission*, which is out of scope. Set
`ACOUSTID_APP_KEY` to override.

### Path template

Edit `/data/config.yaml` (in the `taggerz-data` volume) — `paths.default`,
`paths.singleton`, `paths.comp` — then restart. Default template:
`$albumartist/$album/$track - $title`.

## Safety guarantees

- No writes to audio files without an explicit approve.
- Never deletes files; moves only, on approve.
- Never overwrites: a destination that already exists fails with a
  `destination exists` badge and stays open.
- A file changed on disk between scan and approve is rejected, not overwritten.
- Failed applies leave the file at its original path and the proposal open.

## Development

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/pytest -v
```

Regenerate the test audio fixture: `tests/fixtures/make_fixtures.sh` (needs
`ffmpeg`). Docker build: `docker build -t taggerz .`.
