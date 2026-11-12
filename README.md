# media-check-tools

Self-contained media-import issue tracker for Athena (Unraid). Replaces the old
`Media import check` hourly bash script with a single always-running service.

## What it does

- Polls Radarr/Sonarr import history on a schedule (like the old hourly check).
- Flags problem imports: missing/foreign audio, DD+ 7.1 without an AC-3 core,
  Dolby Vision without an HDR10 fallback, short/truncated runtime, suspiciously
  small files, decode errors, broken/zero-filled downloads.
- Bundles its own ffmpeg/ffprobe — no separate persistent "tools" container,
  no `docker exec` into anything else.
- Web UI lists open issues (what + why) with two actions per item:
  - **Ignore** — dismiss that specific item, logged as acknowledged.
  - **Fix now** — trigger a new Radarr/Sonarr search for that release (the
    same search command the broken-file auto-replace flow already uses).
    Broken/zero-filled files keep being auto-replaced (delete, blocklist,
    search) without needing a button, same as before.
- SQLite for state (replaces `checked.txt` + the daily `report-*.txt` files).

## Explicitly out of scope

- Family watch sync (the Plex-watch-status-to-family-accounts job) stays a
  separate, independent script on Athena — it never depended on this app's
  logic and isn't being folded in.

## Deployment (Athena)

Built and run via Docker Compose from a local clone of this repo, so a
recovery after any docker-folder rebuild is just:

```bash
git pull
docker compose up -d --build
```

No state worth keeping lives in the image — only in the mounted SQLite file
and the compose/Dockerfile source, both of which this repo (or Athena's
appdata share) is the durable copy of.
