# AI backend: manual production setup

This repo's part of the SmartJourney production deployment. The **full step-by-step guide** (AWS account, Lightsail server, Keycloak, data load, backups, decommission) lives in the backend repo:
**[`backend/deploy/MANUAL_SETUP.md`](https://github.com/SmartJourney-SmartTourismProject/backend/blob/main/deploy/MANUAL_SETUP.md)**. Do that one first. This page covers only what's specific to the AI backend.

## How it's deployed

| | |
|---|---|
| Where | A container (`ai-backend`) on the shared Lightsail 4 GB instance, Mumbai |
| Image | `ghcr.io/smartjourney-smarttourismproject/ai-backend:<commit-sha>`, built by this repo's CI on every push to `main` |
| Reachable from | **Only NestJS**, over the server's internal Docker network (`http://ai-backend:8000`). It has **no public URL and no Caddy route**; it's unauthenticated by design. |
| Config | The shared `/opt/smartjourney/.env` on the server (written by hand, see below) |
| Database / cache | The `db` (PostGIS + pgvector) and `redis` containers on the same server |
| Workers | **One** uvicorn worker. The APScheduler data-refresh jobs and the LLM-config refresh loop must not run twice. |

## Pipeline (`.github/workflows/ci.yml`)

On every **push to `main`**:
1. `test`: `pytest -m "not external"` (also runs on pull requests).
2. `build`: builds the `Dockerfile` and pushes `ai-backend:<sha>` and `:latest` to GHCR.
3. `deploy`: over SSH, runs `/opt/smartjourney/deploy.sh ai-backend <sha>` on the server. It pulls the image, restarts the container, waits for `GET /` to answer, and **rolls back to the previous image** if it doesn't come up healthy within about 90 s.

Deploys are serialized (`concurrency: production`), so two pushes never deploy at once.

## Manual steps for this repo

### 1. GitHub secrets
Set as **organization** secrets (full guide §6) and give **this repo** access:

| Secret | Used for |
|---|---|
| `LIGHTSAIL_HOST` | The server's static IP |
| `LIGHTSAIL_USER` | `deploy` |
| `LIGHTSAIL_SSH_KEY` | The CD private key (`smartjourney_deploy`) |

GHCR login uses the built-in `GITHUB_TOKEN`. Also check **Org → Settings → Actions → Workflow permissions = Read and write**.

### 2. Server `.env`: the variables this service reads
Fill these in `/opt/smartjourney/.env` (full guide §4). Copy the provider keys from your local `ai-backend/.env`.

| Variable | Production value |
|---|---|
| `DATABASE_URL` | `postgresql://smartjourney:<POSTGRES_PASSWORD>@db:5432/smartjourney` |
| `REDIS_URL` | `redis://redis:6379/0` |
| `SETTINGS_ENCRYPTION_KEY` | **New** value (`openssl rand -base64 32`). Must match NestJS (same `.env` file). |
| `INTERNAL_API_TOKEN` | **New** random value. NestJS sends it to `/internal/llm/*`. |
| `LLM_PROVIDER_CHAIN` | e.g. `gemini:gemini-3.5-flash-lite,gemini:gemini-3.6-flash,anthropic:claude-haiku-4-5,groq:openai/gpt-oss-120b`. This is the fallback; **Admin → AI models** overrides it at runtime. |
| `GEMINI_API_KEY`, `GROQ_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Provider keys (Anthropic needs credit, see below) |
| `OPENWEATHER_API_KEY`, `ORS_API_KEY`, `BOOKING_RAPIDAPI_KEY`, `BOOKING_RAPIDAPI_HOST`, `TICKETMASTER_API_KEY` | Same as local |
| `EMBEDDING_PROVIDER_CHAIN`, `ENABLE_RAG`, `USD_LKR_RATE` | Same as local |
| `DEBUG` | `false` |

**Google Calendar** (`GOOGLE_CALENDAR_*`) isn't deployed: its OAuth callback would need a public route, and the frontend doesn't use it. Leave those empty.

### 3. Paid model credit (Claude Haiku)
Gemini's free daily quota runs out quickly, so Claude Haiku 4.5 is the reliable backup.
1. <https://console.anthropic.com> → **Billing**: buy credits ($5 covers roughly 150–250 trip plans).
2. **Settings → Limits**: monthly spend limit **$5**, plus an email notification at about $3.
3. **API Keys → Create Key** → put it in the server `.env` as `ANTHROPIC_API_KEY`, or paste it in **Admin → AI models → API keys** after deploy.

### 4. After the first deploy
- **Data:** the database is copied from your local one (full guide §8.2). **Don't run the ingestion connectors** in production; they'd spend API quota re-fetching data that's already there. The scheduled refresh jobs keep running on their own.
- **If Explore shows no places**, listings aren't verified yet. On the server:
  ```bash
  cd /opt/smartjourney
  docker compose -f compose.prod.yml exec ai-backend python -m app.data.verify_all_for_demo
  ```
- **Check models:** **Admin → AI models → Test models.** Expect Working, or "Out of quota" for Gemini late in the day.

## Day-to-day

| Task | How |
|---|---|
| Deploy a change | Merge to `main`. CI tests, builds and deploys automatically. |
| See logs | `ssh deploy@<ip>`, then `cd /opt/smartjourney && docker compose -f compose.prod.yml logs -f --tail 200 ai-backend` |
| Roll back by hand | `./deploy.sh ai-backend <older-commit-sha>` on the server (any SHA that CI built) |
| Restart | `docker compose -f compose.prod.yml restart ai-backend` |
| Change a key or model order | **Admin → AI models** (no restart), or edit `.env` and then `docker compose -f compose.prod.yml up -d ai-backend` |

## Troubleshooting

- **Trip plans say `fallback`:** an LLM problem, not a server problem. **Admin → AI models → Test** shows which provider is out of quota or has a bad key.
- **Deploy job fails at "health check":** the new image didn't start, and `deploy.sh` has already restored the previous one. Read the job log and `docker compose logs ai-backend`.
- **"AI backend isn't reachable" banner in Admin → AI models:** the container is down, or `INTERNAL_API_TOKEN` differs from what NestJS has (they share one `.env`, so check it wasn't edited for one service only).
