# taggerz

A self-hosted web tool that scans a music library for missing metadata,
proposes tags via Chromaprint/AcoustID + beets, then applies the best
match automatically — writing tags and organizing files — while
anything it can't decide waits in a browser queue.

## What it does

1. **Scan** — walks the library, finds audio files missing core metadata
   (artist/album/title/track) or sitting at a path that doesn't match the
   organize template. Read-only.
2. **Fingerprint** — runs Chromaprint (`fpcalc`) and looks up AcoustID →
   MusicBrainz, generating candidate tags via beets.
3. **Queue** — files with no match, failed applies, and anything you
   skip wait in a browser queue grouped by album, with match scores,
   edit forms, retry, and bulk approve-per-album.
4. **Apply** — the best match is applied automatically (tags written,
   file moved into the template path); a destination that already
   exists is replaced, and a file changed on disk since the scan is
   rejected, not clobbered.

## Quick start

```bash
docker compose up
```

This pulls the prebuilt image [`m809/taggerz`](https://hub.docker.com/r/m809/taggerz)
from Docker Hub (or run `docker pull m809/taggerz:latest` yourself). To build
from source instead, use `docker compose up --build`.

Open <http://localhost:8080>. Put your music in `./music` (mounted read-write
at `/music`). The container runs as your `UID`/`GID` (from `.env`, defaulting
to `1000:1000`) so files it writes to the mount stay owned by you.

## Flow

```
Scan (settled files skipped — no tag re-reads) → fingerprint → best
match auto-applied → done
  └─ no match / apply error / your skip → queue (manual approve,
     edit, retry, bulk approve still available)
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

- Scans never write to audio files — they only read tags and stats.
- A file changed on disk between scan and apply is rejected
  (`file changed`), never clobbered.
- Moves replace an existing destination (the displaced copy is
  overwritten); the source file is never deleted in place — it moves.
- Failed applies leave the source at its original path, parked as an
  error, retryable from the queue.
- Anything left in the queue is written only when you approve it.

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
`ffmpeg`). Docker build: `docker build -t m809/taggerz .`.
