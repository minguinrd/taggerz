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
at `/music`). The container runs as your `UID`/`GID` (from `.env`, defaulting
to `1000:1000`) so files it writes to the mount stay owned by you.

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

## Troubleshooting

### Scan fails: `attempt to write a readonly database`

Cause: the data volume (`taggerz-data`) is owned by a different user than the
container runs as. This happens when the compose `user:` directive is added or
changed **after** the volume already exists — the app may boot fine (schema
creation is a no-op), but the first scan can't write. Fix:

```bash
docker compose down
docker run --rm -v taggerz_taggerz-data:/data alpine chown -R 1000:1000 /data
docker compose up -d
```

Replace `1000:1000` with the `UID:GID` from your `user:` line if different.
Since the startup writability probe was added, this mismatch fails at boot
with the same instruction instead of on the first scan click.

## Development

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/pytest -v
```

Regenerate the test audio fixture: `tests/fixtures/make_fixtures.sh` (needs
`ffmpeg`). Docker build: `docker build -t taggerz .`.
