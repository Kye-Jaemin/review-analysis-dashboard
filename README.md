# Review Dashboard

Single-user web app to collect reviews from multiple channels (Google Play, App Store, Reddit, custom URLs), classify them into a user-defined category tree, and grade sentiment on a 5-point scale using Claude — visualized in a dashboard.

Built per the spec in [PROJECT.md](PROJECT.md).

## Features

- **4 source collectors** — Google Play, Apple App Store, Reddit (PRAW official API), custom URLs (static + Playwright dynamic mode).
- **Name-based source discovery** — search by app/subreddit name, pick from candidate cards with icons. No need to enter package IDs manually.
- **User-defined category tree** — multi-level, with per-node descriptions used as LLM classification rubric.
- **5-band sentiment** — `very_positive` / `positive` / `neutral` / `negative` / `very_negative` with integer score 1–5.
- **Model selection at runtime** — pick Haiku/Sonnet/Opus per analysis run; last choice remembered via cookie.
- **Dashboard** — summary cards, sentiment distribution doughnut, per-category stacked bar, time-series trend (5-band or average), per-source breakdown, recent reviews.
- **Reviews explorer** — filters (source / category / sentiment multi-select / date range / keyword) + pagination + CSV/JSON/XLSX export.
- **Bilingual UI** — English & Korean toggle, cookie-persisted.

## Quick start (local)

```bash
git clone <repo>
cd review_analysis
python -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # macOS/Linux

pip install -r requirements.txt

# Optional: dynamic URL scraping (headless Chromium, ~300MB)
pip install -r requirements-playwright.txt
playwright install chromium

cp .env.example .env              # fill in keys you want to use
alembic upgrade head

uvicorn app.main:app --reload
```

Then open <http://localhost:8000>.

### Environment variables (.env)

The app boots without any keys, but each source has a key requirement:

| Source       | Required env vars                                       |
| ------------ | ------------------------------------------------------- |
| Google Play  | none                                                    |
| App Store    | none                                                    |
| Reddit       | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` |
| Custom URL   | optional `PLAYWRIGHT_ENABLED=true` for dynamic pages    |
| AI analysis  | `ANTHROPIC_API_KEY`                                     |

Create a Reddit "script" app at <https://www.reddit.com/prefs/apps> to get `client_id` and `secret`.

## Project layout

```
app/
  main.py             FastAPI app + middleware
  config.py           pydantic settings
  db.py               async SQLAlchemy engine
  jobs.py             in-memory job registry
  templating.py       Jinja2 + i18n render helper
  models/             SQLAlchemy models
  routes/             FastAPI routers (pages, sources, categories, reviews, analyze, export)
  services/
    collectors/       google_play, app_store, reddit, web
    analyzer.py       Claude API integration
    exporter.py       CSV/JSON/XLSX
    stats.py          dashboard aggregations
  i18n/               en.json / ko.json + helpers
  templates/          Jinja2 SSR templates
alembic/              migrations
tests/                pytest suite
```

## Running tests

```bash
pytest
ruff check .
```

## Deploy on Render

This repo includes a `render.yaml` blueprint:

1. Push to GitHub.
2. In Render: **New → Blueprint**, point at your repo.
3. Render provisions a free Postgres + Python web service and runs:
   - `pip install -r requirements.txt && playwright install chromium --with-deps && alembic upgrade head`
   - `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Fill in the `sync: false` secrets (Anthropic + Reddit) in the dashboard.

Render free plan caveats:
- 512 MB RAM — Playwright may OOM; set `PLAYWRIGHT_ENABLED=false` if you hit it.
- App sleeps when idle, first request is slow. In-memory jobs are lost on restart — but collection/analysis are user-triggered, so this is OK.
- Free Postgres is deleted after 90 days; back up your data.

## Current features

- ✅ Scaffolding, i18n, language toggle
- ✅ Data models + categories CRUD with tree
- ✅ 4 source collectors with name-search flow
- ✅ Web collector with static + Playwright dynamic mode
- ✅ AI analysis pipeline with model + summary-language picker
- ✅ Dashboard with 4 charts
- ✅ Reviews explorer + CSV/JSON/XLSX export
- ✅ Render Blueprint
- ✅ pytest + ruff + GitHub Actions CI

## License

Personal use. Each source's ToS is your responsibility to review.
