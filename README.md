# Git Logger

Track changes to web pages and Steam game libraries via git.

## Features

- **Periodic URL fetching** — polls URLs at configurable intervals and commits changes to a git repository
- **Steam game library tracking** — resolves vanity URLs (`/id/{name}/games/`) to profile URLs, extracts full game lists from Steam's SSR data, and pushes to GitHub
- **Multi-profile support** — track any number of Steam profiles (own library size is irrelevant)
- **Dashboard** — web UI at `http://localhost:8814` to add/manage URLs and trigger re-checks
- **Auto-push** — each detected change is committed and pushed to a configurable GitHub repo

## Quick Start

### 1. Clone & configure

```bash
git clone https://github.com/T1bolus/git-logger-sourcecode.git
cd git-logger-sourcecode
cp .env.example .env
```

Edit `.env`:

| Variable | Description |
|----------|-------------|
| `STEAM_USERNAME` | Steam account username |
| `STEAM_PASSWORD` | Steam account password |
| `GITHUB_TOKEN` | GitHub personal access token (repo scope) |
| `GITHUB_REPO` | Target repo, e.g. `T1bolus/git-logger` |

### 2. Run

```bash
docker compose up -d
```

The dashboard is available at `http://<pi-ip>:8814`.

### 3. Steam login

The container will attempt a non-interactive Steam login on startup. If Steam Guard is required:

```bash
curl -X POST http://localhost:8814/api/steam/auth \
  -H "Content-Type: application/json" \
  -d '{"guard_code": "ABC12"}'
```

### 4. Add URLs to track

```bash
curl -X POST http://localhost:8814/api/urls \
  -H "Content-Type: application/json" \
  -d '{"url": "https://steamcommunity.com/id/AlphaDuplo/games/?tab=all", "name": "AlphaDuplo_Steam_Games", "interval": 3600}'
```

Vanity `/id/{name}/` URLs are automatically resolved to `/profiles/{steamid}/` via Steam's public XML endpoint.

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/urls` | List all tracked URLs |
| `POST` | `/api/urls` | Add a URL to track |
| `DELETE` | `/api/urls/{id}` | Remove a tracked URL |
| `POST` | `/api/check-all` | Trigger an immediate check of all URLs |
| `POST` | `/api/steam/auth` | Submit a Steam Guard code |
| `GET` | `/health` | Health check |

## Docker

Built for `linux/arm64` (Raspberry Pi) and `linux/amd64`.

```bash
docker compose build    # build image
docker compose up -d    # start container
docker compose logs -f  # follow logs
```

Data is persisted in `./data/` (bind-mounted to `/app/data`):
- `state.json` — tracked URL configuration
- `repo/` — git repository with change history
- `steamcookies.pkl`, `steam_tokens.json` — Steam session

## Raspberry Pi Deployment

1. Install Docker on the Pi
2. Clone the repo: `git clone https://github.com/T1bolus/git-logger-sourcecode.git`
3. Set up `.env` with credentials
4. (First time only) Copy existing Steam session files from a previous setup or authenticate via the API
5. Run `docker compose up -d`
