# SiftCast

**AI-powered RSS news aggregator that clusters, tags, and explains the news.**

SiftCast pulls articles from dozens of news and tech-blog RSS feeds, groups related coverage into stories using sentence embeddings, classifies every article with an LLM, and generates short bilingual (English + Japanese) background explanations — so you can scan world news in minutes instead of tabs.

**Live:** https://siftcast.pekobit.com

<!-- TODO: add screenshots -->

## Features

- **Multi-source aggregation** — curated master list of news and blog RSS feeds, fetched on a stale-while-revalidate cache so responses stay fast while feeds refresh in the background
- **Story clustering** — articles are embedded with a multilingual sentence-transformer (`paraphrase-multilingual-MiniLM-L12-v2`) and grouped by cosine similarity, so the same story from BBC, AP, and Al Jazeera collapses into one stack ranked by size and recency
- **LLM tagging** — every article is classified into one of 15 fixed categories (Technology, Politics, Conflict, …) plus up to 3 proper-noun topic tags, with output validation and retry
- **Bilingual AI insights** — a 2-sentence background explanation generated in both English and native Japanese per article, with a related YouTube video suggestion
- **On-demand translation** — titles, summaries, and insights translate into Japanese, English, Spanish, or Korean via DeepL, with automatic fallback to Google Translate when the DeepL quota is exhausted; results are cached per language
- **Custom folders** — combine "all articles from these sources" with "only these categories from those sources" filters, with favorites and drag-to-reorder
- **PWA** — installable, with service-worker caching
- **Google OAuth** — sign-in via Google; admin endpoints (feed refresh, batch analysis, debug dashboard) are restricted to the owner account

## How it works

```
RSS feeds ──► fetch & dedupe (guid + title similarity)
                    │
                    ▼
              ArticleCache (SQLite, WAL)
                    │
        ┌───────────┴───────────┐  background worker pool (4 threads)
        ▼                       ▼
  LLM classification      sentence embedding
  (category + topics)     (MiniLM, normalized)
        │                       │
        ▼                       ▼
  bilingual insights      star clustering by
  (EN + JA, via LLM)      cosine similarity
                    │
                    ▼
        scored story stacks ──► API ──► PWA frontend
```

- Feed refresh uses a 30-minute TTL with stale-while-revalidate: requests always return cached data immediately while a single background thread re-fetches feeds, queues AI jobs, and prunes articles older than 7 days.
- AI jobs (tagging, insights, embeddings) run on a bounded thread pool to cap concurrent LLM API calls and SQLite writers.
- Cheap models (Gemma) handle bulk pre-generation; a stronger model (Gemini Flash) is used for on-demand analysis.

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python, Flask, SQLAlchemy, SQLite (WAL), gunicorn |
| AI / NLP | OpenRouter (Gemini 2.5 Flash, Gemma 3), sentence-transformers, DeepL & Google Translate APIs |
| Auth | Google OAuth 2.0 (Authlib) + Flask-Login |
| Frontend | Single-file vanilla JS PWA, Tailwind CSS |
| Infra | Hetzner Cloud VPS (Ubuntu 24.04), nginx, Let's Encrypt (certbot), Cloudflare |

## Local development

```bash
git clone https://github.com/Peko-Peko-Bit/siftcast-public.git
cd SiftCast
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env   # then fill in the keys — see comments in the file
python app.py          # http://127.0.0.1:5001
```

Required keys: `SECRET_KEY`, Google OAuth credentials (`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REDIRECT_URI`). AI features additionally need `OPENROUTER_API_KEY`; translation needs `DEEPL_API_KEY` (and optionally `GOOGLE_TRANSLATE_API_KEY` as fallback); related videos need `YOUTUBE_API_KEY`. The app runs without the optional keys — the corresponding features degrade gracefully.

## Deployment

Production runs as a systemd service (gunicorn, single worker — in-process caching and the background worker pool assume one process) behind nginx with TLS from Let's Encrypt. The nginx config template lives in [deploy/](deploy/):

- [deploy/nginx.conf](deploy/nginx.conf) — nginx site config (HTTPS blocks managed by `certbot --nginx`)

The systemd unit is not included in the repository — a minimal unit that runs `gunicorn -c gunicorn.conf.py app:app` with `Restart=always` is all that's needed.

To ship an update:

```bash
ssh <server>
cd ~/siftcast && git pull && sudo systemctl restart siftcast
```

Certificate renewal is automatic via `certbot.timer`. Logs: `/var/log/siftcast/error.log`.
