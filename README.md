# abs-stats

A Tautulli-style stats dashboard for [Audiobookshelf](https://www.audiobookshelf.org/). It syncs listening data from your Audiobookshelf server into a local SQLite database and serves its own web UI.

## Features

- **Activity:** Tautulli-style cards for everyone who listened since the last sync: what they're listening to, where in the book they started, how long they've listened, client, device, stream type and IP address. Refreshes on its own after each sync
- **Home:** listening totals, most listened and most popular titles, top authors, listeners, clients and genres, recent activity, recently added and finished books
- **History:** searchable, filterable session log with start and end position in the book and calculated playback speed
- **Graphs:** listening by hour, day or month (stacked by user), day of week, hour of day, client, device, genre and play method
- **Users and titles:** per-user and per-book pages with progress, totals and history
- **Libraries:** item counts, runtime, size and listening per library
- **Themes:** several light and dark themes, plus holiday themes that can switch on automatically
- **Notifications (optional):** Discord or ntfy messages when someone finishes a book or new items are added

History is stored locally, so it survives Audiobookshelf cleanups and migrations.

## Requirements

- Audiobookshelf 2.x
- Docker (Docker Compose, Portainer, Synology Container Manager, etc.)

## Setup

1. **Create a service account** in Audiobookshelf: Settings > Users > add a user named `abs-stats` with type **Admin** (needed to read every user's sessions). Don't listen to anything with it.
2. **Get its API token** from that user's page (or log in as it and check its Account page). Newer Audiobookshelf versions have an API Keys page under Settings instead.
3. **Configure:**
   ```sh
   git clone <this repo> abs-stats && cd abs-stats
   cp .env.example .env                              # then fill in ABS_URL and ABS_TOKEN
   cp docker-compose.example.yml docker-compose.yml  # then set user and TZ
   mkdir data                                        # must be writable by the user in the compose file
   ```
4. **Start:** `docker compose up -d --build`

   Or skip building: a prebuilt image (amd64 and arm64) is published to `ghcr.io/krzysztof-cloud/abs-stats:latest` on every push to `main`. Replace the `build:` section with `image: ghcr.io/krzysztof-cloud/abs-stats:latest`.
5. **Open** `http://<host>:8484`. The first sync runs on startup and imports existing history.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ABS_URL` | required | Audiobookshelf URL reachable from the container |
| `ABS_TOKEN` | required | API token of the admin service account |
| `POLL_INTERVAL` | `300` | Seconds between syncs of sessions, progress and activity. Failed syncs retry after 60 seconds |
| `ITEMS_INTERVAL` | `1800` | Seconds between full library scans (new items, metadata) |
| `ACTIVE_WINDOW` | `300` | Seconds since last activity before a reader drops off the Activity panel |
| `STATS_USERNAME` | `abs-stats` | Service account name, excluded from user stats |
| `TZ` | `America/New_York` | Timezone for daily and hourly grouping |
| `WEB_PORT` | `8484` | Port the web UI listens on |
| `DB_PATH` | `/data/abs-stats.db` | Database location inside the container |
| `DISCORD_WEBHOOK_URL` | empty | Optional Discord notifications |
| `NTFY_URL` | empty | Optional ntfy topic URL |

## Security

- The web UI has **no login**. Keep it on your LAN or put it behind a reverse proxy with authentication.
- The API token gives admin access to Audiobookshelf. It lives only in `.env`, which is git-ignored. Never commit it.

## Notes

- **Playback speed** is calculated as book time covered divided by real listening time. Sessions where the listener skipped or rewound show `n/a`.
- **Activity** shows anyone Audiobookshelf heard from in the last `ACTIVE_WINDOW` seconds (default 5 minutes, plus a minute for sync lag). Cards drop off on their own once someone stops. Open sessions that are still being updated are marked "Listening now". It is not a live position tracker.
- Covers are fetched from Audiobookshelf and cached in `data/covers`.

## License

MIT. Bundles [Chart.js](https://www.chartjs.org/) (MIT).

## Changelog

- **1.2.1:** Activity only shows readers active in the last 5 minutes and clears itself; abandoned open sessions no longer stick
- **1.2:** Activity panel with connection details, 5-minute syncs (library scans stay at 30 minutes), page refreshes itself after each sync
- **1.1:** 24 and 48 hour ranges, hourly graphs, start/end position and speed in History, circular logo, themes and holiday themes
- **1.0:** First release
