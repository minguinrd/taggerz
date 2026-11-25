# taggerz

A self-hosted web tool that scans a music library for missing metadata,
proposes tags via Chromaprint/AcoustID + [beets](https://beets.io), then
applies the best match automatically — writing tags and organizing files —
while anything it can't decide waits in a browser review queue.

Source: <https://github.com/minguinrd/taggerz>

## Quick start (docker compose)

```yaml
services:
  taggerz:
    image: m809/taggerz:latest
    container_name: taggerz
    restart: unless-stopped
    ports: ["127.0.0.1:8080:8080"]
    user: "1000:1000"          # your UID:GID, so written files stay yours
    volumes:
      - /path/to/music:/music  # your library (read-write)
      - taggerz-data:/data     # queue database + beets config
    environment:
      ACOUSTID_APP_KEY: ""     # optional override
volumes:
  taggerz-data:
```

```bash
docker compose up -d
```

Open <http://localhost:8080>.

## Quick start (docker run)

```bash
docker run -d --name taggerz --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  --user "$(id -u):$(id -g)" \
  -v /path/to/music:/music \
  -v taggerz-data:/data \
  m809/taggerz:latest
```

## What it does

1. **Scan** — finds audio files missing core metadata (artist/album/title/
   track) or sitting at a path that doesn't match the organize template.
   Read-only.
2. **Fingerprint** — runs Chromaprint (`fpcalc`) and looks up AcoustID →
   MusicBrainz, generating candidate tags via beets.
3. **Apply** — the best match is applied automatically (tags written, file
   moved into the template path).
4. **Queue** — no match, failed applies, and anything you skip wait in a
   browser queue grouped by album, with match scores, edit forms, retry and
   bulk approve-per-album.

## Configuration

| Variable           | Default   | Notes                                  |
|--------------------|-----------|----------------------------------------|
| `MUSIC_DIR`        | `/music`  | Library path inside the container      |
| `DATA_DIR`         | `/data`   | SQLite queue + beets config            |
| `PORT`             | `8080`    | Listen port inside the container       |
| `HOST`             | `0.0.0.0` | Listen address                         |
| `ACOUSTID_APP_KEY` | *(beets)* | Optional; beets' embedded key is used  |

**Path template:** edit `/data/config.yaml` (`paths.default`,
`paths.singleton`, `paths.comp`) and restart. Default:
`$albumartist/$album/$track - $title`.

## Safety

- Scans never write to audio files.
- A file changed on disk between scan and apply is rejected, not clobbered.
- Failed applies leave the source at its original path, retryable from the
  queue.

## Troubleshooting

**`attempt to write a readonly database` / startup fails on `/data`:** the
data volume is owned by a different UID than the container runs as (e.g. you
changed `user:` after the volume was created). Fix:

```bash
docker compose down
docker run --rm -v taggerz_taggerz-data:/data alpine chown -R 1000:1000 /data
docker compose up -d
```

## Tags

- `latest` — current `main`
- `<short-sha>` — a specific commit
