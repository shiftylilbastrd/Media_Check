# media-check-tools

Self-contained media-import issue tracker for Athena (Unraid). Replaces the old
`Media import check` hourly bash script with a single always-running service.

## What it does

- **Primary trigger: webhooks.** Radarr/Sonarr Connect → Webhook, fired on
  "On Import"/"On Upgrade", POSTs to `/webhooks/radarr` and `/webhooks/sonarr`
  here. Checks run within seconds of an import instead of up to an hour later.
- **Fallback: a once-a-day poll** of the last ~26h of import history, purely
  as a safety net for a missed webhook delivery (arr restart, network blip,
  this app being down at that moment). Not the primary mechanism anymore.
- Flags problem imports: missing/foreign audio, Dolby Vision without an
  HDR10 fallback, short/truncated runtime, suspiciously small files, decode
  errors, broken/zero-filled downloads.
- Bundles its own ffmpeg/ffprobe — no separate persistent "tools" container,
  no `docker exec` into anything else.
- **Broken/zero-filled files are still fully automatic**: delete the file
  record, remove it from disk, blocklist the release, trigger a new search —
  no button, same as the old script (capped at 10/day; past that, logged as
  an open issue to check manually instead of silently repeating forever).
- **Web UI** lists everything else (the softer issues - foreign-only audio,
  DV w/o HDR10, runtime/size oddities, decode errors) with two actions:
  - **Ignore** — dismiss that specific item, logged as acknowledged. Sticks
    even if the same file triggers another webhook later.
  - **Fix now** — trigger a new Radarr/Sonarr search for that release (same
    search command the broken-file auto-replace uses). Doesn't delete or
    blocklist the current file — only the automatic broken-file path does
    that, since those other issues aren't necessarily unplayable.
- **Settings page** for Radarr/Sonarr/Plex URL + API key/token — entered
  there directly, not auto-read from each app's own config.xml. Same pattern
  every other arr-ecosystem app (Cleanuparr, Maintainerr, Prowlarr, …) uses.
- SQLite for state (replaces `checked.txt` + the daily `report-*.txt` files).

## Known gaps

- **DD+ 7.1-without-an-AC3-core detection and its auto-fix are not ported.**
  The old script's `eac3_layout()` (from SABnzbd's `sab_media_gate.py`) and
  `fix_dd71.py` (which muxed in a Shield-compatible DD+ 5.1 track) were never
  pasted into this project, and guessing at the exact ffprobe-stream logic or
  ffmpeg mux args for something that rewrites media files unattended isn't a
  risk worth taking. Pending those two files' actual source.
- Webhook payload field names (`movie`, `movieFile.mediaInfo`, `series`,
  `episodeFile.mediaInfo`, etc.) are based on Radarr/Sonarr's documented
  webhook schema, not yet verified against a real **Test** webhook from this
  install. Every payload is logged raw to `/data/webhook-raw.log` regardless
  of whether parsing succeeds — check that log after sending a real Test
  event from each app's Connect settings, and adjust `app/webhooks.py` if
  any field names don't match.

## Explicitly out of scope

- Family watch sync (the Plex-watch-status-to-family-accounts job) stays a
  separate, independent script on Athena — it never depended on this app's
  logic and isn't being folded in.

## Setup

1. `docker compose up -d --build` (see below).
2. Open the UI (port 8585) → **Settings** → enter Radarr/Sonarr/Plex URL +
   API key/token.
3. In Radarr and Sonarr: **Settings → Connect → Add → Webhook**, URL
   `http://<athena-ip>:8585/webhooks/radarr` (or `/sonarr`), trigger on
   **On Import** and **On Upgrade**. Hit **Test** once and check
   `/data/webhook-raw.log` to confirm the payload parses as expected.

## Deployment (Athena)

Built and run via Docker Compose from a local clone of this repo, so a
recovery after any docker-folder rebuild is just:

```bash
git pull
docker compose up -d --build
```

No state worth keeping lives in the image — only in the mounted SQLite file
(and the raw webhook debug log) under `/data`, and the compose/Dockerfile
source, both of which this repo (or Athena's appdata share) is the durable
copy of.
