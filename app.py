import os
import re
import json
import time
import difflib
import threading
from concurrent.futures import ThreadPoolExecutor
import requests
import feedparser
import embedder
from flask import Flask, request, jsonify, redirect
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_login import LoginManager, current_user, login_user, logout_user
from functools import wraps
from authlib.integrations.flask_client import OAuth
from authlib.integrations.base_client.errors import OAuthError
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
from models import db, User, MasterFeed, ArticleCache, Folder, ArticleTranslation, format_model_name
from master_feeds import MASTER_FEEDS
from translation import translate_text, translate_titles as _translate_titles_batch
from datetime import datetime, timedelta
from sqlalchemy import inspect, text, or_, and_
import math

# Load environment variables
load_dotenv(override=True)

app = Flask(__name__, static_folder='.', static_url_path='')
# Behind nginx (with Cloudflare in front): trust X-Forwarded-* from the local proxy
# so Flask sees the real scheme/host/client IP
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Allow CORS: restrict to ALLOWED_ORIGIN in production, open in development
_allowed_origin = os.getenv('ALLOWED_ORIGIN', '*')
CORS(app,
     resources={
         r"/api/*":  {"origins": _allowed_origin},
         r"/auth/*": {"origins": _allowed_origin},
     },
     supports_credentials=True)

@app.before_request
def log_request():
    print(f"Incoming Request: {request.method} {request.path}")

# Database Configuration
# Default to local dev.db for SQLite if no URL is provided
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///dev.db')
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
if DATABASE_URL.startswith('sqlite'):
    # Wait up to 15s on a locked database instead of failing immediately
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'connect_args': {'timeout': 15}}

db.init_app(app)

# Session security — KeyError on missing SECRET_KEY to prevent insecure startup
app.config['SECRET_KEY'] = os.environ['SECRET_KEY']
_cookie_secure = os.getenv('FLASK_ENV') != 'development'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = _cookie_secure
# Flask-Login's long-lived remember cookie needs its own settings
app.config['REMEMBER_COOKIE_SECURE'] = _cookie_secure
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'

login_manager = LoginManager(app)
login_manager.login_view = None  # Return 401 JSON instead of redirecting

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@login_manager.unauthorized_handler
def unauthorized():
    return jsonify({"error": "Authentication required"}), 401

oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.environ['GOOGLE_CLIENT_ID'],
    client_secret=os.environ['GOOGLE_CLIENT_SECRET'],
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

OWNER_GOOGLE_ID = os.getenv('OWNER_GOOGLE_ID')

def owner_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "Authentication required"}), 401
        if not OWNER_GOOGLE_ID or current_user.google_id != OWNER_GOOGLE_ID:
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return decorated

OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_MODEL_INSIGHTS = os.getenv('OPENROUTER_MODEL_INSIGHTS', 'google/gemini-2.5-flash')
OPENROUTER_MODEL_INSIGHTS_BATCH = os.getenv('OPENROUTER_MODEL_INSIGHTS_BATCH', 'google/gemma-3-27b-it')
OPENROUTER_MODEL_TAGS = os.getenv('OPENROUTER_MODEL_TAGS', 'google/gemma-3-12b-it')
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


def seed_data():
    """Sync MasterFeed table with MASTER_FEEDS list (single source of truth)."""
    canonical_urls = {f["rss_url"] for f in MASTER_FEEDS}

    for f in MASTER_FEEDS:
        existing = MasterFeed.query.filter_by(name=f["name"]).first() \
                   or MasterFeed.query.filter_by(rss_url=f["rss_url"]).first()
        if existing:
            existing.name = f["name"]
            existing.rss_url = f["rss_url"]
            existing.category = f["category"]
            existing.is_featured = f["is_featured"]
            existing.feed_type = f.get("feed_type", "news")
        else:
            db.session.add(MasterFeed(
                name=f["name"],
                rss_url=f["rss_url"],
                category=f["category"],
                is_featured=f["is_featured"],
                feed_type=f.get("feed_type", "news"),
            ))
    db.session.flush()

    # Remove legacy feeds not in MASTER_FEEDS
    for feed in MasterFeed.query.all():
        if feed.rss_url not in canonical_urls:
            db.session.delete(feed)

    db.session.commit()

    # Purge articles (and their translations) orphaned by feed removal —
    # ORM delete above doesn't cascade to ArticleCache and SQLite doesn't enforce FKs
    with db.engine.connect() as conn:
        conn.execute(text(
            "DELETE FROM article_translation WHERE article_id IN "
            "(SELECT id FROM article_cache WHERE feed_id NOT IN (SELECT id FROM master_feed))"))
        result = conn.execute(text(
            "DELETE FROM article_cache WHERE feed_id NOT IN (SELECT id FROM master_feed)"))
        conn.commit()
    if result.rowcount:
        print(f"Seed: removed {result.rowcount} articles from deleted feeds")

    # Drop obsolete user_subscription table if it still exists
    with db.engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS user_subscription"))
        conn.commit()

def migrate_db():
    """Add new columns to existing tables if they don't exist."""
    inspector = inspect(db.engine)

    master_feed_columns = [c['name'] for c in inspector.get_columns('master_feed')]
    if 'is_featured' not in master_feed_columns:
        with db.engine.connect() as conn:
            conn.execute(text("ALTER TABLE master_feed ADD COLUMN is_featured BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()
        print("DB migration: added is_featured column to master_feed")
    if 'feed_type' not in master_feed_columns:
        with db.engine.connect() as conn:
            conn.execute(text("ALTER TABLE master_feed ADD COLUMN feed_type TEXT DEFAULT 'news'"))
            conn.execute(text("UPDATE master_feed SET feed_type = 'blog' WHERE id IN (25, 26, 28, 29, 30, 34, 36)"))
            conn.commit()
        print("DB migration: added feed_type column to master_feed")

    if 'folder' in inspector.get_table_names():
        folder_columns = [c['name'] for c in inspector.get_columns('folder')]
        if 'tag_sources' not in folder_columns:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE folder ADD COLUMN tag_sources TEXT"))
                conn.commit()
            print("DB migration: added tag_sources column to folder")

    # Rename semantic_tags → category_tag
    try:
        with db.engine.connect() as conn:
            conn.execute(text("ALTER TABLE article_cache RENAME COLUMN semantic_tags TO category_tag"))
            conn.commit()
        print("DB migration: renamed semantic_tags to category_tag")
    except Exception:
        pass  # already renamed or column doesn't exist

    # Clear old-format values (containing "#") so backfill regenerates them
    try:
        with db.engine.connect() as conn:
            conn.execute(text("UPDATE article_cache SET category_tag = NULL WHERE category_tag LIKE '#%'"))
            conn.commit()
    except Exception:
        pass

    article_cache_columns = [c['name'] for c in inspector.get_columns('article_cache')]
    new_cols = {
        'thumbnail_url': 'VARCHAR(512)',
        'source_lang': 'VARCHAR(10)',
        'insights_lang': 'VARCHAR(10)',
        'summary_translated': 'TEXT',
        'insights_translated': 'TEXT',
        'translated_lang': 'VARCHAR(10)',
        'topic_tags': 'VARCHAR(255)',
        'translated_engine': 'VARCHAR(20)',
        'embedding': 'TEXT',
        'insights_model': 'TEXT',
        'ai_insights_ja': 'TEXT',
    }
    for col, coltype in new_cols.items():
        if col not in article_cache_columns:
            with db.engine.connect() as conn:
                conn.execute(text(f"ALTER TABLE article_cache ADD COLUMN {col} {coltype}"))
                conn.commit()
            print(f"DB migration: added {col} column to article_cache")

    # folder.user_id migration (Step 1: auth schema)
    if 'folder' in inspector.get_table_names():
        folder_columns = [c['name'] for c in inspector.get_columns('folder')]
        if 'user_id' not in folder_columns:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE folder ADD COLUMN user_id INTEGER REFERENCES users(id)"))
                conn.commit()
            print("DB migration: added user_id column to folder")

    # Remove orphaned translations left by past bulk deletes (FKs not enforced in SQLite)
    if 'article_translation' in inspector.get_table_names():
        with db.engine.connect() as conn:
            result = conn.execute(text(
                "DELETE FROM article_translation "
                "WHERE article_id NOT IN (SELECT id FROM article_cache)"))
            conn.commit()
        if result.rowcount:
            print(f"DB migration: removed {result.rowcount} orphaned article_translation rows")

ARTICLE_MAX_AGE_DAYS = 7  # retention horizon shared by cleanup and ingestion guard

def cleanup_old_articles(max_days=ARTICLE_MAX_AGE_DAYS):
    """Delete ArticleCache rows (and their translations) older than max_days.
    Bulk deletes bypass ORM cascade and SQLite doesn't enforce FKs, so the
    ArticleTranslation rows must be removed explicitly."""
    cutoff = datetime.utcnow() - timedelta(days=max_days)
    old_ids = [row.id for row in ArticleCache.query
               .filter(ArticleCache.published_at < cutoff)
               .with_entities(ArticleCache.id).all()]
    if not old_ids:
        return
    ArticleTranslation.query.filter(
        ArticleTranslation.article_id.in_(old_ids)
    ).delete(synchronize_session=False)
    deleted = ArticleCache.query.filter(
        ArticleCache.id.in_(old_ids)
    ).delete(synchronize_session=False)
    db.session.commit()
    print(f"Cleanup: deleted {deleted} articles older than {max_days} days")

# --- Initial Setup ---
with app.app_context():
    if DATABASE_URL.startswith('sqlite'):
        from sqlalchemy import event

        @event.listens_for(db.engine, 'connect')
        def _set_sqlite_pragma(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute('PRAGMA journal_mode=WAL')
            cur.execute('PRAGMA synchronous=NORMAL')
            cur.close()

    db.create_all()
    migrate_db()
    seed_data()
    cleanup_old_articles()


def _backfill_embeddings():
    """Generate embeddings for articles that don't have one yet."""
    with app.app_context():
        missing = ArticleCache.query.filter(
            ArticleCache.embedding == None
        ).order_by(ArticleCache.published_at.desc()).all()
        if not missing:
            return
        print(f"[embedder] Backfilling {len(missing)} articles...")
        for article in missing:
            try:
                text_input = article.title
                if article.summary:
                    text_input += ". " + article.summary[:500]
                article.embedding = json.dumps(embedder.encode(text_input))
            except Exception as e:
                print(f"[embedder] Backfill error for article {article.id}: {e}")
        db.session.commit()
        print("[embedder] Backfill complete.")


# Preload model and backfill missing embeddings in the background
embedder.preload()
threading.Thread(target=_backfill_embeddings, daemon=True).start()


# --- Thumbnail Extraction ---
def parse_published_at(entry):
    """Extract publication datetime from RSS entry. Returns None if the feed
    provides no date, so callers can distinguish 'unknown' from 'now'."""
    pub = entry.get('published_parsed') or entry.get('updated_parsed')
    if pub:
        try:
            return datetime(*pub[:6])
        except Exception:
            pass
    return None


def extract_thumbnail(entry):
    """Extract thumbnail URL from an RSS entry. Priority: media_thumbnail > media_content > enclosures > img in summary."""
    # 1. media:thumbnail
    media_thumbnail = getattr(entry, 'media_thumbnail', None)
    if media_thumbnail:
        url = media_thumbnail[0].get('url')
        if url:
            return url

    # 2. media:content (image type)
    media_content = getattr(entry, 'media_content', None)
    if media_content:
        for mc in media_content:
            mc_type = mc.get('type', '')
            mc_url = mc.get('url', '')
            if mc_type.startswith('image/') or re.search(r'\.(jpe?g|png|webp|gif)(\?|$)', mc_url, re.I):
                return mc_url

    # 3. enclosures (image type)
    enclosures = getattr(entry, 'enclosures', None)
    if enclosures:
        for enc in enclosures:
            if enc.get('type', '').startswith('image/'):
                return enc.get('href') or enc.get('url')

    # 4. <img> tag in summary HTML
    summary = entry.get('summary', '') or ''
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary, re.I)
    if match:
        return match.group(1)

    return None


# --- AI Integration ---
def _openrouter_headers():
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:5001",
        "X-Title": "SiftCast"
    }

VALID_CATEGORIES = {
    "Technology", "Politics", "Business", "Economy", "Science",
    "Health", "Environment", "Conflict", "Crime", "Society",
    "Education", "Sports", "Entertainment", "Regional", "Other"
}

def _is_valid_tags(tags_data: dict) -> bool:
    if tags_data.get("category_tag") not in VALID_CATEGORIES:
        return False
    if len(tags_data.get("topic_tags", [])) > 3:
        return False
    return True

def generate_tags(title, content_snippet):
    """Classify article into a fixed category and extract topic tags."""
    if not OPENROUTER_API_KEY:
        return {"category_tag": "Other", "topic_tags": []}

    system_prompt = """You are a news article classifier. Output ONLY valid JSON. No explanation, no markdown, no code fences.

CATEGORY LIST (pick exactly one):
Technology, Politics, Business, Economy, Science, Health, Environment,
Conflict, Crime, Society, Education, Sports, Entertainment, Regional, Other

RULES:
- category_tag: EXACTLY ONE from the category list above. Do NOT invent new categories.
  · "Conflict"  — wars, armed conflicts, terrorism, military strikes
  · "Crime"     — criminal cases, arrests, trials, cybercrime
  · "Society"   — immigration, human rights, gender, inequality, religion (citizen-focused)
  · "Politics"  — government policy, elections, diplomacy (institution-focused)
  · "Regional"  — local or small-nation news with limited global impact
                   (e.g. local elections in a small country, regional disasters,
                   community stories from Africa, Southeast Asia, etc.)
  · "Other"     — only when no category fits

- topic_tags: 0 to 3 proper nouns ONLY — people, companies, organizations, products.
  Format: "#Name" (use underscore for spaces, e.g. "#Elon_Musk").
  Countries, cities, and regions are NOT topic_tags — omit them.
  If none, use [].

OUTPUT FORMAT (strict):
{"category_tag": "Technology", "topic_tags": ["#Samsung", "#Apple"]}"""

    user_prompt = f"Classify this article:\nTitle: {title}\nSummary: {content_snippet}"

    data = {
        "model": OPENROUTER_MODEL_TAGS,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "response_format": {"type": "json_object"},
    }

    tags_data = {}
    for attempt in range(2):
        try:
            response = requests.post(OPENROUTER_URL, headers=_openrouter_headers(), json=data, timeout=30)
            response.raise_for_status()
            content = response.json()['choices'][0]['message']['content'].strip()
            tags_data = json.loads(content)
            if _is_valid_tags(tags_data):
                break
        except Exception as e:
            print(f"Tag Generation Error (attempt {attempt + 1}): {e}")

    return {
        "category_tag": tags_data.get("category_tag") if tags_data.get("category_tag") in VALID_CATEGORIES else "Other",
        "topic_tags": tags_data.get("topic_tags", []) if isinstance(tags_data.get("topic_tags"), list) else []
    }


def generate_insights(title, content_snippet, lang=None, model=None):
    """Generate insights and YouTube query via OpenRouter."""
    if not OPENROUTER_API_KEY:
        return {
            "insights": "Please set OPENROUTER_API_KEY to enable AI insights.",
            "youtube_query": title
        }

    use_model = model or OPENROUTER_MODEL_INSIGHTS
    lang_names = {"ja": "Japanese", "en": "English", "es": "Spanish"}
    lang_instruction = f" Write the insights in {lang_names[lang]}." if lang in lang_names else ""

    prompt = f"""Analyze the following news article and return a JSON object.
Title: {title}
Snippet: {content_snippet}

Return format:
{{
    "insights": "A 2-sentence explanation of the background/context of this article.{lang_instruction}",
    "youtube_query": "Keyword for searching related videos on YouTube (in the same language as the article)"
}}"""

    data = {
        "model": use_model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"}
    }
    try:
        response = requests.post(OPENROUTER_URL, headers=_openrouter_headers(), json=data, timeout=30)
        response.raise_for_status()
        content = response.json()['choices'][0]['message']['content']
        if not content or not content.strip():
            print(f"Insights Generation Error ({use_model}): empty content in response")
            raise ValueError("empty content")
        return json.loads(content)
    except Exception as e:
        print(f"Insights Generation Error ({use_model}): {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"Insights Response body: {e.response.text[:300]}")
        return {
            "insights": "Analysis failed briefly. Please try again later.",
            "youtube_query": title
        }


# --- Static Frontend ---
@app.route('/')
def index():
    return app.send_static_file('index.html')


# ─── Auth Routes ─────────────────────────────────────────────────────────────

@app.route('/auth/login')
def auth_login():
    redirect_uri = os.environ['GOOGLE_REDIRECT_URI']
    return google.authorize_redirect(redirect_uri)


@app.route('/auth/callback')
def auth_callback():
    try:
        token = google.authorize_access_token()
    except OAuthError:
        return redirect('/')
    info = token.get('userinfo') or google.userinfo()

    user = User.query.filter_by(google_id=info['sub']).first()
    if user is None:
        user = User(
            google_id=info['sub'],
            email=info.get('email'),
            display_name=info.get('name'),
            avatar_url=info.get('picture'),
        )
        db.session.add(user)
    else:
        user.email = info.get('email')
        user.display_name = info.get('name')
        user.avatar_url = info.get('picture')
    db.session.commit()

    login_user(user, remember=True)
    return redirect('/')


@app.route('/auth/logout', methods=['POST'])
def auth_logout():
    logout_user()
    return jsonify({"ok": True})


@app.route('/auth/me')
def auth_me():
    if not current_user.is_authenticated:
        return jsonify({"authenticated": False, "is_owner": False})
    is_owner = bool(OWNER_GOOGLE_ID and current_user.google_id == OWNER_GOOGLE_ID)
    return jsonify({"authenticated": True, "is_owner": is_owner, **current_user.to_dict()})


# --- API Endpoints ---

@app.route('/api/search', methods=['GET'])
def search_feeds():
    query = request.args.get('q', '')
    if not query:
        feeds = MasterFeed.query.all()
    else:
        feeds = MasterFeed.query.filter(MasterFeed.name.ilike(f'%{query}%')).all()
    return jsonify([f.to_dict() for f in feeds])


# Bounded worker pool for per-article AI jobs (tagging / insights).
# Caps concurrent OpenRouter requests and SQLite writers; previously each
# new article spawned its own threads (hundreds during a full refresh).
AI_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix='ai-worker')

_inflight_lock = threading.Lock()
_inflight_jobs = set()  # {('tag', article_id), ('insights', article_id)}


def _submit_ai_job(kind, fn, article_id, *args):
    """Submit an AI job unless the same (kind, article_id) is already queued/running.
    Prevents the periodic backfill from double-queueing articles whose job is still
    in flight, and logs job exceptions (otherwise silently swallowed by the executor)."""
    key = (kind, article_id)
    with _inflight_lock:
        if key in _inflight_jobs:
            return False
        _inflight_jobs.add(key)

    def _done(future):
        with _inflight_lock:
            _inflight_jobs.discard(key)
        exc = future.exception()
        if exc:
            print(f"[ai-worker] {kind} job failed for article {article_id}: {exc!r}", flush=True)

    AI_EXECUTOR.submit(fn, article_id, *args).add_done_callback(_done)
    return True


def tag_and_cache(article_id, title, summary):
    """Background thread: generate category_tag + topic_tags, plus embedding."""
    results = {}

    def _get_tags():
        results['tags'] = generate_tags(title, summary)

    def _get_embedding():
        text_input = title
        if summary:
            text_input += ". " + summary[:500]
        try:
            results['embedding'] = json.dumps(embedder.encode(text_input))
        except Exception as e:
            print(f"[embedder] Encode error: {e}")
            results['embedding'] = None

    t1 = threading.Thread(target=_get_tags)
    t2 = threading.Thread(target=_get_embedding)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    with app.app_context():
        cached = ArticleCache.query.get(article_id)
        if cached:
            tags_data = results.get('tags', {})
            cached.category_tag = tags_data.get('category_tag', 'Other')
            cached.topic_tags = json.dumps(tags_data.get('topic_tags', []), ensure_ascii=False)
            cached.embedding = results.get('embedding')
            db.session.commit()


def analyze_and_cache(article_id, title, summary, lang=None, model=None):
    """Background thread: generate EN + JA insights in parallel + YouTube query."""
    with app.app_context():
        use_model = model or OPENROUTER_MODEL_INSIGHTS
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_en = executor.submit(generate_insights, title, summary, lang='en', model=use_model)
            future_ja = executor.submit(generate_insights, title, summary, lang='ja', model=use_model)
            en_data = future_en.result()
            ja_data = future_ja.result()
        cached = ArticleCache.query.get(article_id)
        if cached:
            cached.ai_insights = en_data.get('insights')
            cached.youtube_query = en_data.get('youtube_query')
            cached.ai_insights_ja = ja_data.get('insights')
            cached.insights_lang = 'en'
            cached.insights_model = use_model
            db.session.commit()


@app.route('/api/news/<int:feed_id>', methods=['GET'])
def get_news(feed_id):
    master_feed = MasterFeed.query.get_or_404(feed_id)

    now = datetime.utcnow()
    last = _feed_last_fetched.get(feed_id)
    needs_refresh = not last or (now - last).total_seconds() > FEED_CACHE_TTL

    articles = ArticleCache.query.filter_by(feed_id=feed_id)\
        .order_by(ArticleCache.published_at.desc()).limit(10).all()

    if not articles:
        # First access: fetch synchronously so we don't return empty
        _fetch_and_cache_feed(feed_id)
        _feed_last_fetched[feed_id] = now
        articles = ArticleCache.query.filter_by(feed_id=feed_id)\
            .order_by(ArticleCache.published_at.desc()).limit(10).all()
    elif needs_refresh:
        # Stale cache: return DB data immediately, refresh in background
        _feed_last_fetched[feed_id] = now  # optimistic update to avoid duplicate fetches
        t = threading.Thread(target=_fetch_and_cache_feed, args=(feed_id,))
        t.daemon = True
        t.start()

    return jsonify({
        "source": master_feed.name,
        "articles": [a.to_dict() for a in articles]
    })


@app.route('/api/tags/<int:article_id>', methods=['GET'])
def get_tags(article_id):
    """Poll endpoint: returns category + topic tags once generation is complete."""
    cached = ArticleCache.query.get_or_404(article_id)
    if cached.category_tag:
        return jsonify({
            "ready": True,
            "category_tag": cached.category_tag,
            "topic_tags": cached._parse_topic_tags()
        })
    return jsonify({"ready": False})


@app.route('/api/insights/<int:article_id>', methods=['GET'])
def get_insights(article_id):
    """Poll endpoint: returns AI insights once background analysis is complete."""
    cached = ArticleCache.query.get_or_404(article_id)
    if cached.ai_insights:
        return jsonify({
            "ready": True,
            "ai_insights": cached.ai_insights,
            "ai_insights_ja": cached.ai_insights_ja,
            "category_tag": cached.category_tag,
            "youtube_query": cached.youtube_query,
            "insights_model": format_model_name(cached.insights_model),
        })
    return jsonify({"ready": False})


_featured_last_fetched = None
# Interval of the server-side periodic refresh loop (seconds). Overridable for local testing.
FEATURED_REFRESH_INTERVAL = int(os.getenv('FEATURED_REFRESH_INTERVAL', '1800'))
# Per-cycle caps for re-queueing articles whose AI jobs were lost (queue is
# memory-only, so a restart drops pending jobs). Insights cost 2 LLM calls per
# article, hence the tighter cap; large backlogs drain over a few cycles.
TAGS_BACKFILL_PER_CYCLE = 50
INSIGHTS_BACKFILL_PER_CYCLE = 30
_featured_refresh_running = False
_stacked_cache = None  # {'data': [...], 'ts': datetime}

_feed_last_fetched = {}  # feed_id → datetime
FEED_CACHE_TTL = 300  # 5 minutes


def _fetch_and_cache_feed(master_feed_id):
    """Fetch a single feed by ID and cache any new articles. Safe to run in a thread."""
    with app.app_context():
        master_feed = MasterFeed.query.get(master_feed_id)
        if not master_feed:
            return
        feed = feedparser.parse(master_feed.rss_url)
        raw_lang = getattr(feed.feed, 'language', None)
        ingest_cutoff = datetime.utcnow() - timedelta(days=ARTICLE_MAX_AGE_DAYS)
        for entry in feed.entries[:10]:
            guid = entry.get('id') or entry.get('link')
            if ArticleCache.query.filter_by(guid=guid).first():
                continue
            pub = parse_published_at(entry)
            if pub and pub < ingest_cutoff:
                continue  # already past the retention horizon; don't ingest
            entry_lang = getattr(entry, 'language', None) or raw_lang
            source_lang = entry_lang[:2].lower() if entry_lang else None
            cached = ArticleCache(
                feed_id=master_feed_id,
                guid=guid,
                title=entry.title,
                link=entry.link,
                summary=entry.get('summary', ''),
                published_at=pub or datetime.utcnow(),
                thumbnail_url=extract_thumbnail(entry),
                source_lang=source_lang
            )
            db.session.add(cached)
            try:
                db.session.commit()
                _submit_ai_job('tag', tag_and_cache, cached.id, entry.title, entry.get('summary', ''))
                _submit_ai_job('insights', analyze_and_cache, cached.id, entry.title, entry.get('summary', ''),
                               None, OPENROUTER_MODEL_INSIGHTS_BATCH)
            except Exception:
                db.session.rollback()


def _refresh_featured_feeds():
    """Fetch RSS for all featured feeds and save new articles. Returns True if fetch ran."""
    global _featured_last_fetched
    now = datetime.utcnow()
    if _featured_last_fetched and (now - _featured_last_fetched).total_seconds() < FEATURED_REFRESH_INTERVAL:
        return False

    # Purge expired articles before fetching so cleanup can't race with
    # ingestion of the same cycle (runs at most every FEATURED_REFRESH_INTERVAL)
    try:
        cleanup_old_articles()
    except Exception as e:
        db.session.rollback()
        print(f"Cleanup error: {e}")

    featured_feeds = MasterFeed.query.filter_by(is_featured=True).all()
    featured_ids = {f.id for f in featured_feeds}

    # Collect feeds referenced in any folder (target_sources + tag_sources)
    folder_feed_ids = set()
    for folder in Folder.query.all():
        for fid in (json.loads(folder.target_sources) if folder.target_sources else []):
            folder_feed_ids.add(fid)
        for fid in (json.loads(folder.tag_sources) if folder.tag_sources else []):
            folder_feed_ids.add(fid)
    extra_feeds = MasterFeed.query.filter(
        MasterFeed.id.in_(folder_feed_ids - featured_ids)
    ).all() if folder_feed_ids - featured_ids else []

    all_feeds = featured_feeds + extra_feeds
    new_entries = []
    ingest_cutoff = datetime.utcnow() - timedelta(days=ARTICLE_MAX_AGE_DAYS)

    for master_feed in all_feeds:
        feed = feedparser.parse(master_feed.rss_url)
        raw_lang = getattr(feed.feed, 'language', None)
        for entry in feed.entries[:10]:
            guid = entry.get('id') or entry.get('link')
            existing = ArticleCache.query.filter_by(guid=guid).first()
            if existing:
                dirty = False
                if existing.thumbnail_url is None:
                    existing.thumbnail_url = extract_thumbnail(entry)
                    dirty = True
                pub = parse_published_at(entry)
                if pub is not None and existing.published_at != pub:
                    existing.published_at = pub
                    dirty = True
                if dirty:
                    db.session.commit()
                continue
            pub = parse_published_at(entry)
            if pub and pub < ingest_cutoff:
                continue  # already past the retention horizon; don't ingest
            is_dup = False
            for _, dup_entry, _ in new_entries:
                if difflib.SequenceMatcher(None, entry.title, dup_entry.title).ratio() >= 0.95:
                    is_dup = True
                    break
            if not is_dup:
                entry_lang = getattr(entry, 'language', None) or raw_lang
                new_entries.append((master_feed.id, entry, entry_lang))

    for feed_id, entry, raw_lang in new_entries:
        guid = entry.get('id') or entry.get('link')
        source_lang = raw_lang[:2].lower() if raw_lang else None
        cached = ArticleCache(
            feed_id=feed_id,
            guid=guid,
            title=entry.title,
            link=entry.link,
            summary=entry.get('summary', ''),
            published_at=parse_published_at(entry) or datetime.utcnow(),
            thumbnail_url=extract_thumbnail(entry),
            source_lang=source_lang
        )
        db.session.add(cached)
        db.session.commit()
        _submit_ai_job('tag', tag_and_cache, cached.id, entry.title, entry.get('summary', ''))
        _submit_ai_job('insights', analyze_and_cache, cached.id, entry.title, entry.get('summary', ''),
                       None, OPENROUTER_MODEL_INSIGHTS_BATCH)

    # Backfill articles whose AI jobs were lost (e.g. dropped from the in-memory
    # queue by a restart). Covers ALL cached articles, featured feeds included.
    # The in-flight set inside _submit_ai_job keeps this from double-queueing the
    # new entries submitted just above. Runs after new-entry submission so fresh
    # articles keep queue priority.
    untagged = ArticleCache.query.filter(
        or_(ArticleCache.category_tag == None, ArticleCache.category_tag == '')
    ).order_by(ArticleCache.published_at.desc()).limit(TAGS_BACKFILL_PER_CYCLE).all()
    for article in untagged:
        _submit_ai_job('tag', tag_and_cache, article.id, article.title, article.summary or '')

    uninsighted = ArticleCache.query.filter(
        ArticleCache.ai_insights == None
    ).order_by(ArticleCache.published_at.desc()).limit(INSIGHTS_BACKFILL_PER_CYCLE).all()
    for article in uninsighted:
        _submit_ai_job('insights', analyze_and_cache, article.id, article.title, article.summary or '',
                       None, OPENROUTER_MODEL_INSIGHTS_BATCH)

    _featured_last_fetched = now

    return True


def _refresh_featured_feeds_bg():
    """Run _refresh_featured_feeds in a background thread, then invalidate stacked cache."""
    global _featured_refresh_running, _stacked_cache
    try:
        with app.app_context():
            _refresh_featured_feeds()
    finally:
        _stacked_cache = None
        _featured_refresh_running = False


_refresh_flag_lock = threading.Lock()


def _ensure_featured_seeded():
    """First-deploy safety net: fetch synchronously when no featured article exists yet,
    so the very first request doesn't return an empty feed. No-op once the periodic
    refresh loop has completed (or is running) a cycle."""
    if _featured_last_fetched is not None or _featured_refresh_running:
        return
    if ArticleCache.query.filter(
        ArticleCache.feed_id.in_(
            [f.id for f in MasterFeed.query.filter_by(is_featured=True).all()])
    ).first():
        return
    _refresh_featured_feeds()


def _scheduler_loop():
    """Periodic server-side refresh: RSS fetch + AI job queueing + cleanup, then sleep.
    Replaces the old request-driven stale-while-revalidate — endpoints now only read
    what this loop has already prepared in the DB."""
    global _featured_refresh_running
    while True:
        with _refresh_flag_lock:
            already_running = _featured_refresh_running
            if not already_running:
                _featured_refresh_running = True
        if not already_running:
            print("[scheduler] Feed refresh cycle starting...", flush=True)
            _refresh_featured_feeds_bg()
            print("[scheduler] Feed refresh cycle finished.", flush=True)
        time.sleep(FEATURED_REFRESH_INTERVAL)


def _start_scheduler():
    # Under the werkzeug dev reloader the module is imported twice; only the
    # reloaded child (WERKZEUG_RUN_MAIN=true) should run the loop. In production
    # (gunicorn, single worker) neither env var is set and the loop starts here.
    if os.getenv('FLASK_ENV') == 'development' and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return
    threading.Thread(target=_scheduler_loop, daemon=True, name='feed-scheduler').start()


_start_scheduler()


@app.route('/api/analyze/batch', methods=['POST'])
@owner_required
def trigger_analyze_batch():
    """Batch pre-generate insights for multiple articles using the cheaper batch model."""
    req_data = request.get_json(silent=True) or {}
    article_ids = req_data.get('article_ids', [])
    lang = req_data.get('target_lang')

    if not article_ids:
        return jsonify({"ok": True, "queued": 0})

    queued = 0
    for article_id in article_ids:
        cached = ArticleCache.query.get(article_id)
        if not cached or cached.ai_insights:
            continue
        if _submit_ai_job('insights', analyze_and_cache, cached.id, cached.title, cached.summary or '',
                          lang, OPENROUTER_MODEL_INSIGHTS_BATCH):
            queued += 1

    return jsonify({"ok": True, "queued": queued})


@app.route('/debug')
@owner_required
def debug_page():
    """Admin debug page: feed/folder/cache/tag-generation status."""
    JST_OFFSET = timedelta(hours=9)
    now_utc = datetime.utcnow()
    now_jst = now_utc + JST_OFFSET

    # --- 1. Feed state ---
    feeds = MasterFeed.query.order_by(MasterFeed.id).all()
    feed_rows = []
    for f in feeds:
        total = ArticleCache.query.filter_by(feed_id=f.id).count()
        latest_row = ArticleCache.query.filter_by(feed_id=f.id).order_by(ArticleCache.published_at.desc()).first()
        latest = latest_row.published_at.strftime('%Y-%m-%d %H:%M') if latest_row and latest_row.published_at else '—'
        untagged = ArticleCache.query.filter(
            ArticleCache.feed_id == f.id,
            or_(ArticleCache.category_tag == None, ArticleCache.category_tag == '')
        ).count()
        feed_rows.append((f.id, f.name, f.category, total, latest, untagged))

    # --- 2. Folder state ---
    folders = Folder.query.order_by(Folder.id).all()
    feed_name_map = {f.id: f.name for f in feeds}
    cutoff7 = now_utc - timedelta(days=7)
    folder_rows = []
    for fo in folders:
        ts  = json.loads(fo.target_sources) if fo.target_sources else []
        tgs = json.loads(fo.tag_sources)    if fo.tag_sources    else []
        tgt = json.loads(fo.target_tags)    if fo.target_tags    else []
        conditions = []
        if ts:
            conditions.append(ArticleCache.feed_id.in_(ts))
        if tgs and tgt:
            conditions.append(and_(ArticleCache.feed_id.in_(tgs), ArticleCache.category_tag.in_(tgt)))
        if conditions:
            count = ArticleCache.query.filter(
                or_(*conditions),
                ArticleCache.published_at >= cutoff7
            ).count()
        else:
            count = 0
        ts_names  = ', '.join(feed_name_map.get(i, str(i)) for i in ts)  or '—'
        tgs_names = ', '.join(feed_name_map.get(i, str(i)) for i in tgs) or '—'
        tgt_str   = ', '.join(tgt) or '—'
        folder_rows.append((fo.id, fo.name, fo.is_favorite, ts_names, tgs_names, tgt_str, count))

    # --- 3. Cache / refresh state ---
    if _featured_last_fetched:
        last_jst = _featured_last_fetched + JST_OFFSET
        last_str = last_jst.strftime('%Y-%m-%d %H:%M:%S JST')
        elapsed  = (now_utc - _featured_last_fetched).total_seconds()
        remaining_min = max(0, (FEATURED_REFRESH_INTERVAL - elapsed) / 60)
        remaining_str = f'{remaining_min:.1f} min'
    else:
        last_str = 'Never'
        remaining_str = 'On next request'

    tag_source_feed_ids = set()
    for fo in folders:
        for fid in (json.loads(fo.tag_sources) if fo.tag_sources else []):
            tag_source_feed_ids.add(fid)
    total_untagged = ArticleCache.query.filter(
        ArticleCache.feed_id.in_(tag_source_feed_ids),
        or_(ArticleCache.category_tag == None, ArticleCache.category_tag == '')
    ).count() if tag_source_feed_ids else 0

    # --- 4. Per-feed tag generation status (tag_sources only) ---
    tag_gen_rows = []
    for fid in sorted(tag_source_feed_ids):
        fname = feed_name_map.get(fid, str(fid))
        total_a = ArticleCache.query.filter_by(feed_id=fid).count()
        tagged  = ArticleCache.query.filter(
            ArticleCache.feed_id == fid,
            ArticleCache.category_tag != None,
            ArticleCache.category_tag != ''
        ).count()
        tag_gen_rows.append((fid, fname, total_a, tagged, total_a - tagged))

    # --- HTML ---
    th = 'padding:6px 10px;text-align:left;border-bottom:1px solid #ddd;white-space:nowrap'
    td = 'padding:5px 10px;border-bottom:1px solid #f0f0f0;font-size:13px'
    td_r = td + ';text-align:right'
    def tbl(headers, rows, right_cols=None):
        right_cols = right_cols or set()
        head = ''.join(f'<th style="{th}">{h}</th>' for h in headers)
        body = ''
        for r in rows:
            body += '<tr>' + ''.join(
                f'<td style="{td_r if i in right_cols else td}">{v}</td>'
                for i, v in enumerate(r)
            ) + '</tr>'
        return f'<table style="border-collapse:collapse;width:100%"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'

    feed_tbl   = tbl(['ID','Name','Category','Cached','Latest published_at','Untagged'], feed_rows, {0,3,5})
    folder_tbl = tbl(['ID','Name','Fav','target_sources','tag_sources','target_tags','Matches (7d)'], folder_rows, {0,6})
    tag_tbl    = tbl(['Feed ID','Feed Name','Total','Tagged','Untagged'], tag_gen_rows, {0,2,3,4})

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>SiftCast Debug</title>
<style>body{{font-family:sans-serif;margin:24px;color:#222}}h2{{margin-top:32px;border-bottom:2px solid #4f46e5;padding-bottom:4px;color:#4f46e5}}table{{margin-bottom:8px}}</style>
</head><body>
<h1 style="color:#111">SiftCast Debug</h1>
<p style="color:#666;font-size:13px">Last checked: <strong>{now_jst.strftime('%Y-%m-%d %H:%M:%S JST')}</strong>
&nbsp;<a href="/debug" style="background:#4f46e5;color:#fff;padding:4px 12px;border-radius:4px;text-decoration:none;font-size:12px">Reload</a>
&nbsp;<button id="refresh-btn" onclick="triggerRefresh()" style="background:#059669;color:#fff;padding:4px 12px;border-radius:4px;border:none;cursor:pointer;font-size:12px">Refresh now</button></p>
<script>
async function triggerRefresh() {{
    const btn = document.getElementById('refresh-btn');
    btn.textContent = 'Refreshing...';
    btn.disabled = true;
    btn.style.opacity = '0.6';
    try {{ await fetch('/debug/refresh', {{method:'POST'}}); }} catch(e) {{}}
    setTimeout(() => location.reload(), 3000);
}}
</script>

<h2>1. Feed status</h2>
{feed_tbl}

<h2>2. Folder status</h2>
{folder_tbl}

<h2>3. Cache &amp; refresh status</h2>
<table style="border-collapse:collapse">
<tr><td style="{td}"><strong>_featured_last_fetched</strong></td><td style="{td}">{last_str}</td></tr>
<tr><td style="{td}"><strong>Next refresh in</strong></td><td style="{td}">{remaining_str}</td></tr>
<tr><td style="{td}"><strong>Untagged articles in tag_sources (total)</strong></td><td style="{td_r}">{total_untagged}</td></tr>
</table>

<h2>4. Tag generation (tag_sources feeds)</h2>
{tag_tbl if tag_gen_rows else '<p style="color:#999;font-size:13px">No feeds registered as tag_sources</p>'}
</body></html>"""

    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route('/debug/refresh', methods=['POST'])
@owner_required
def debug_refresh():
    """Immediately reset TTL and kick off a background refresh (with app context)."""
    global _featured_last_fetched, _featured_refresh_running
    _featured_last_fetched = None
    with _refresh_flag_lock:
        if not _featured_refresh_running:
            _featured_refresh_running = True
            threading.Thread(target=_refresh_featured_feeds_bg, daemon=True).start()
    return jsonify({"status": "started"})


@app.route('/api/analyze/<int:article_id>', methods=['POST'])
@owner_required
def trigger_analyze(article_id):
    """Force re-generate insights for a specific article. Clears existing insights and translations."""
    cached = ArticleCache.query.get_or_404(article_id)
    cached.ai_insights = None
    cached.ai_insights_ja = None
    cached.youtube_query = None
    ArticleTranslation.query.filter_by(article_id=article_id).update({'insights': None})
    db.session.commit()
    _submit_ai_job('insights', analyze_and_cache, cached.id, cached.title, cached.summary or '')
    return jsonify({"ok": True})


@app.route('/api/refresh', methods=['POST'])
@owner_required
def force_refresh():
    """Immediately kick off a background RSS refresh (same behavior as /debug/refresh)."""
    global _featured_last_fetched, _featured_refresh_running
    _featured_last_fetched = None
    with _refresh_flag_lock:
        if not _featured_refresh_running:
            _featured_refresh_running = True
            threading.Thread(target=_refresh_featured_feeds_bg, daemon=True).start()
    return jsonify({"status": "started"})


@app.route('/api/refresh/status', methods=['GET'])
def refresh_status():
    """Lightweight poll endpoint: reports the last completed refresh cycle."""
    return jsonify({
        "last_refreshed": _featured_last_fetched.isoformat() if _featured_last_fetched else None,
        "running": _featured_refresh_running,
    })


EMBED_THRESHOLD = 0.60  # cosine similarity cutoff for grouping

def _group_articles(articles):
    """Sequential star clustering using embedding cosine similarity.
    Each article joins the first open group whose leader exceeds EMBED_THRESHOLD.
    Articles without an embedding become singletons."""
    sorted_articles = sorted(
        articles,
        key=lambda a: a.published_at or datetime.min,
        reverse=True,
    )

    groups = []        # list of [leader, member, ...]
    leader_vecs = []   # parallel list: parsed embedding of each group leader

    for article in sorted_articles:
        if not article.embedding:
            groups.append([article])
            leader_vecs.append(None)
            continue

        vec = json.loads(article.embedding)
        placed = False
        for i, group in enumerate(groups):
            if len(group) >= 5 or leader_vecs[i] is None:
                continue
            if embedder.cosine(vec, leader_vecs[i]) >= EMBED_THRESHOLD:
                group.append(article)
                placed = True
                break
        if not placed:
            groups.append([article])
            leader_vecs.append(vec)

    return groups


@app.route('/api/news/stacked', methods=['GET'])
def get_stacked_news():
    """Featured articles grouped by shared tags, scored by group size / recency."""
    global _stacked_cache

    now_utc = datetime.utcnow()
    _ensure_featured_seeded()

    # Return memory-cached grouped result; invalidated at the end of each refresh cycle
    if _stacked_cache:
        return jsonify(_stacked_cache['data'])

    cutoff_news = now_utc - timedelta(days=2)
    cutoff_blog = now_utc - timedelta(days=7)

    featured_feeds = MasterFeed.query.filter_by(is_featured=True).all()
    news_ids = [f.id for f in featured_feeds if f.feed_type != 'blog']
    blog_ids = [f.id for f in featured_feeds if f.feed_type == 'blog']

    type_conditions = []
    if news_ids:
        type_conditions.append(and_(ArticleCache.feed_id.in_(news_ids), ArticleCache.published_at >= cutoff_news))
    if blog_ids:
        type_conditions.append(and_(ArticleCache.feed_id.in_(blog_ids), ArticleCache.published_at >= cutoff_blog))
    if not type_conditions:
        return jsonify([])

    articles = ArticleCache.query.filter(
        or_(*type_conditions)
    ).order_by(ArticleCache.published_at.desc()).limit(500).all()

    result = []
    for group in _group_articles(articles):
        group_sorted = sorted(group, key=lambda a: a.published_at or datetime.min, reverse=True)
        latest = group_sorted[0]
        hours_since = (now_utc - latest.published_at).total_seconds() / 3600 if latest.published_at else 24
        score = len(group) / (hours_since + 1)
        result.append({
            "score": round(score, 3),
            "main": latest.to_dict(),
            "related": [a.to_dict() for a in group_sorted[1:]]
        })

    result.sort(key=lambda x: x["score"], reverse=True)
    data = result[:30]
    _stacked_cache = {'data': data, 'ts': now_utc}
    return jsonify(data)


@app.route('/api/news/by-tag', methods=['GET'])
def get_by_tag_news():
    """Featured articles grouped by semantic tag, with embedded grouping nested inside."""
    from collections import defaultdict
    _ensure_featured_seeded()

    now_utc = datetime.utcnow()
    cutoff_news = now_utc - timedelta(days=2)
    cutoff_blog = now_utc - timedelta(days=7)

    featured_feeds = MasterFeed.query.filter_by(is_featured=True).all()
    news_ids = [f.id for f in featured_feeds if f.feed_type != 'blog']
    blog_ids = [f.id for f in featured_feeds if f.feed_type == 'blog']

    type_conditions = []
    if news_ids:
        type_conditions.append(and_(ArticleCache.feed_id.in_(news_ids), ArticleCache.published_at >= cutoff_news))
    if blog_ids:
        type_conditions.append(and_(ArticleCache.feed_id.in_(blog_ids), ArticleCache.published_at >= cutoff_blog))
    if not type_conditions:
        return jsonify([])

    articles = ArticleCache.query.filter(
        or_(*type_conditions)
    ).order_by(ArticleCache.published_at.desc()).limit(500).all()

    tag_to_articles = defaultdict(list)
    for article in articles:
        if article.category_tag:
            tag_to_articles[article.category_tag].append(article)

    result = []
    for tag, tag_articles in tag_to_articles.items():
        groups = _group_articles(tag_articles)
        stacks = []
        for group in groups:
            group_sorted = sorted(group, key=lambda a: a.published_at or datetime.min, reverse=True)
            latest = group_sorted[0]
            hours_since = (now_utc - latest.published_at).total_seconds() / 3600 if latest.published_at else 24
            score = len(group) / (hours_since + 1)
            stacks.append({
                "score": round(score, 3),
                "main": latest.to_dict(),
                "related": [a.to_dict() for a in group_sorted[1:]]
            })

        # Primary: most related articles; secondary: highest score (recency-weighted)
        stacks.sort(key=lambda x: (len(x['related']), x['score']), reverse=True)
        result.append({
            "tag": tag,
            "count": len(stacks),
            "header_title": stacks[0]['main']['title'] if stacks else '',
            "stacks": stacks
        })

    result.sort(key=lambda x: x['count'], reverse=True)
    return jsonify(result)


@app.route('/api/news/featured', methods=['GET'])
def get_featured_news():
    """Returns deduped latest articles from all is_featured feeds."""
    _ensure_featured_seeded()

    now_utc = datetime.utcnow()
    cutoff_news = now_utc - timedelta(days=2)
    cutoff_blog = now_utc - timedelta(days=7)

    featured_feeds = MasterFeed.query.filter_by(is_featured=True).all()
    news_ids = [f.id for f in featured_feeds if f.feed_type != 'blog']
    blog_ids = [f.id for f in featured_feeds if f.feed_type == 'blog']

    type_conditions = []
    if news_ids:
        type_conditions.append(and_(ArticleCache.feed_id.in_(news_ids), ArticleCache.published_at >= cutoff_news))
    if blog_ids:
        type_conditions.append(and_(ArticleCache.feed_id.in_(blog_ids), ArticleCache.published_at >= cutoff_blog))
    if not type_conditions:
        return jsonify({"articles": []})

    articles = ArticleCache.query.filter(
        or_(*type_conditions)
    ).order_by(ArticleCache.published_at.desc()).limit(30).all()

    return jsonify({"articles": [a.to_dict() for a in articles]})


@app.route('/api/article/<int:article_id>/youtube', methods=['GET'])
def get_youtube_video(article_id):
    """Returns a cached or freshly fetched YouTube video ID for the article."""
    cached = ArticleCache.query.get_or_404(article_id)

    if cached.youtube_video_id:
        return jsonify({"video_id": cached.youtube_video_id})

    if not YOUTUBE_API_KEY:
        return jsonify({"video_id": None, "error": "no_api_key"})

    if not cached.youtube_query:
        return jsonify({"video_id": None, "error": "not_found"})

    try:
        params = {
            "key": YOUTUBE_API_KEY,
            "q": cached.youtube_query,
            "part": "snippet",
            "type": "video",
            "maxResults": 1,
            "relevanceLanguage": "en"
        }
        response = requests.get(YOUTUBE_SEARCH_URL, params=params, timeout=10)
        response.raise_for_status()
        items = response.json().get("items", [])
        if not items:
            return jsonify({"video_id": None, "error": "not_found"})

        video_id = items[0]["id"]["videoId"]
        cached.youtube_video_id = video_id
        db.session.commit()
        return jsonify({"video_id": video_id})
    except Exception as e:
        print(f"YouTube API Error: {e}")
        return jsonify({"video_id": None, "error": "not_found"})

SUPPORTED_LANGS = {'ja', 'en', 'es', 'ko'}


@app.route('/api/translate/<int:article_id>', methods=['POST'])
def translate_article(article_id):
    """Translate article summary and insights. Results cached in ArticleTranslation per lang."""
    data = request.json
    target_lang = data.get('target_lang')

    if target_lang not in SUPPORTED_LANGS:
        return jsonify({"error": "invalid target_lang"}), 400

    cached = ArticleCache.query.get_or_404(article_id)

    # JA has native insights; EN needs no translation; others require translation engine
    has_native_ja = target_lang == 'ja' and bool(cached.ai_insights_ja)
    needs_insights_translation = target_lang != 'en' and not has_native_ja

    # Check per-language cache
    tr = ArticleTranslation.query.filter_by(article_id=article_id, lang=target_lang).first()
    insights_cache_ok = (
        not needs_insights_translation
        or (tr and tr.insights is not None)
        or (not cached.ai_insights)
    )
    if tr and tr.summary is not None and insights_cache_ok:
        if has_native_ja:
            insights_out = cached.ai_insights_ja or ''
        elif needs_insights_translation:
            insights_out = (tr.insights if tr else None) or ''
        else:
            insights_out = cached.ai_insights or ''
        return jsonify({"summary": tr.summary, "insights": insights_out})

    engine = data.get('engine', 'auto')
    # Translation API auto-detects source language; no need for source_lang check
    result, engine_used = translate_text(cached.summary or '', target_lang, engine=engine)
    summary_ok = engine_used != "none"

    insights_result = None
    insights_ok = False
    if has_native_ja:
        translated_insights = cached.ai_insights_ja or ''
    elif needs_insights_translation and cached.ai_insights and (tr is None or tr.insights is None):
        insights_result, insights_engine = translate_text(cached.ai_insights, target_lang, engine=engine)
        insights_ok = insights_engine != "none"
        translated_insights = insights_result
    else:
        translated_insights = cached.ai_insights or ''

    # Cache only successful translations — otherwise a quota outage would pin
    # the untranslated original in ArticleTranslation forever
    if summary_ok or insights_ok:
        if tr is None:
            tr = ArticleTranslation(article_id=article_id, lang=target_lang)
            db.session.add(tr)
        if summary_ok:
            tr.summary = result
            tr.engine = engine_used
        if insights_ok:
            tr.insights = insights_result
        db.session.commit()

    return jsonify({"summary": result, "insights": translated_insights})


# Worst legitimate case: first load of the stacked view with translation on
# sends every visible title at once (stacks are built from up to 500 articles)
TITLES_MAX_PER_REQUEST = 500
# ArticleCache.title is String(255); defensive cap on what we send to engines
TITLE_TRANSLATE_MAX_CHARS = 300


@app.route('/api/translate-titles', methods=['POST'])
def translate_titles():
    """Translate a batch of article titles. Checks ArticleTranslation cache first.
    The text sent to translation engines is always read from ArticleCache —
    client-supplied title strings are never translated or cached, so this
    endpoint can't be used to translate arbitrary text or poison the cache."""
    data = request.json or {}
    target_lang = data.get('target_lang')
    requested = data.get('titles', [])  # [{"id": 123, "title": "..."}, ...]

    if target_lang not in SUPPORTED_LANGS:
        return jsonify({"error": "invalid target_lang"}), 400
    if not requested:
        return jsonify({}), 200
    if not isinstance(requested, list) or len(requested) > TITLES_MAX_PER_REQUEST:
        return jsonify({"error": "too many titles"}), 400

    article_ids = []
    client_title = {}  # echoed back (untranslated) for ids we don't know
    for t in requested:
        try:
            aid = int(t['id'])
        except (KeyError, TypeError, ValueError):
            continue
        if aid not in client_title:
            article_ids.append(aid)
        client_title[aid] = str(t.get('title') or '')
    if not article_ids:
        return jsonify({}), 200

    # Server-side source of truth for the text to translate
    title_by_id = {
        a.id: a.title for a in
        ArticleCache.query.filter(ArticleCache.id.in_(article_ids)).all()
    }

    # Load existing translation rows for these articles (cached titles may be
    # None if the row was created by a summary translation first)
    existing = ArticleTranslation.query.filter(
        ArticleTranslation.article_id.in_(article_ids),
        ArticleTranslation.lang == target_lang,
    ).all()
    existing_by_id = {tr.article_id: tr for tr in existing}
    result_map = {tr.article_id: tr.title for tr in existing if tr.title is not None}

    def _respond():
        # Fall back to the article's real title, then to the client's copy
        return jsonify({
            str(aid): result_map.get(aid) or title_by_id.get(aid) or client_title.get(aid, '')
            for aid in article_ids
        })

    to_translate = [
        aid for aid in article_ids
        if aid not in result_map and title_by_id.get(aid)
    ]
    if to_translate:
        texts = [title_by_id[aid][:TITLE_TRANSLATE_MAX_CHARS] for aid in to_translate]
        translated = _translate_titles_batch(texts, target_lang)
        if translated is None:
            # All engines failed — respond with originals but don't cache them
            return _respond()
        for aid, translated_title in zip(to_translate, translated):
            result_map[aid] = translated_title
            tr = existing_by_id.get(aid)
            if tr is None:
                tr = ArticleTranslation(article_id=aid, lang=target_lang)
                db.session.add(tr)
            tr.title = translated_title
        db.session.commit()

    return _respond()


# --- Folder APIs ---

@app.route('/api/folders', methods=['GET'])
def get_folders():
    folders = Folder.query.order_by(Folder.sort_order, Folder.id).all()
    return jsonify([f.to_dict() for f in folders])


@app.route('/api/folders', methods=['POST'])
@owner_required
def create_folder():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    folder = Folder(
        name=name,
        target_sources=json.dumps(data.get('target_sources', [])),
        tag_sources=json.dumps(data.get('tag_sources', [])),
        target_tags=json.dumps(data.get('target_tags', [])),
        is_favorite=bool(data.get('is_favorite', False)),
        sort_order=int(data.get('sort_order', 0)),
    )
    db.session.add(folder)
    db.session.commit()
    return jsonify(folder.to_dict()), 201


@app.route('/api/folders/<int:folder_id>', methods=['PUT'])
@owner_required
def update_folder(folder_id):
    folder = Folder.query.get_or_404(folder_id)
    data = request.json or {}
    if 'name' in data:
        name = (data['name'] or '').strip()
        if not name:
            return jsonify({"error": "name cannot be empty"}), 400
        folder.name = name
    if 'target_sources' in data:
        folder.target_sources = json.dumps(data['target_sources'])
    if 'tag_sources' in data:
        folder.tag_sources = json.dumps(data['tag_sources'])
    if 'target_tags' in data:
        folder.target_tags = json.dumps(data['target_tags'])
    if 'is_favorite' in data:
        folder.is_favorite = bool(data['is_favorite'])
    if 'sort_order' in data:
        folder.sort_order = int(data['sort_order'])
    db.session.commit()
    return jsonify(folder.to_dict())


@app.route('/api/folders/<int:folder_id>', methods=['DELETE'])
@owner_required
def delete_folder(folder_id):
    folder = Folder.query.get_or_404(folder_id)
    db.session.delete(folder)
    db.session.commit()
    return jsonify({"ok": True})


@app.route('/api/folders/<int:folder_id>/favorite', methods=['PATCH'])
@owner_required
def toggle_folder_favorite(folder_id):
    folder = Folder.query.get_or_404(folder_id)
    folder.is_favorite = not folder.is_favorite
    db.session.commit()
    return jsonify(folder.to_dict())


@app.route('/api/folders/reorder', methods=['PUT'])
@owner_required
def reorder_folders():
    """Body: [{"id": 1, "sort_order": 0}, ...]"""
    items = request.json or []
    for item in items:
        folder = Folder.query.get(item.get('id'))
        if folder:
            folder.sort_order = int(item['sort_order'])
    db.session.commit()
    return jsonify({"ok": True})


def _compute_trending_scores(articles):
    """Return {article.id: float} trending score for each article."""
    now = datetime.utcnow()

    # Aggregate tag stats across the article set
    tag_stats = {}  # tag -> {"count": int, "feed_ids": set}
    for a in articles:
        tags = [t.strip() for t in (a.topic_tags or '').split(',') if t.strip()]
        for tag in tags:
            if tag not in tag_stats:
                tag_stats[tag] = {"count": 0, "feed_ids": set()}
            tag_stats[tag]["count"] += 1
            tag_stats[tag]["feed_ids"].add(a.feed_id)

    scores = {}
    for a in articles:
        tags = [t.strip() for t in (a.topic_tags or '').split(',') if t.strip()]
        if not tags:
            scores[a.id] = 0.0
            continue
        pub = a.published_at or now
        hours = max((now - pub).total_seconds() / 3600, 0.1)
        decay = 1.0 / math.log(1 + hours)
        tag_scores = []
        for tag in tags:
            stats = tag_stats[tag]
            diversity_bonus = len(stats["feed_ids"]) * 0.2
            tag_scores.append(stats["count"] * decay * diversity_bonus)
        scores[a.id] = max(tag_scores)

    return scores


@app.route('/api/folders/<int:folder_id>/articles', methods=['GET'])
def get_folder_articles(folder_id):
    """Articles matching the folder's source/tag filters. ?sort=latest|trending"""
    _ensure_featured_seeded()
    folder = Folder.query.get_or_404(folder_id)

    target_sources = json.loads(folder.target_sources) if folder.target_sources else []
    tag_sources    = json.loads(folder.tag_sources)    if folder.tag_sources    else []
    target_tags    = json.loads(folder.target_tags)    if folder.target_tags    else []

    conditions = []
    # Group 1: target_sources → show all articles regardless of tag
    if target_sources:
        conditions.append(ArticleCache.feed_id.in_(target_sources))
    # Group 2: tag_sources → show only articles matching target_tags
    if tag_sources and target_tags:
        conditions.append(and_(
            ArticleCache.feed_id.in_(tag_sources),
            ArticleCache.category_tag.in_(target_tags)
        ))

    if not conditions:
        return jsonify({"articles": []})

    # Fetch any source feeds that have never been cached
    all_folder_feed_ids = list(set(target_sources + tag_sources))
    never_fetched = [
        fid for fid in all_folder_feed_ids
        if not ArticleCache.query.filter(ArticleCache.feed_id == fid).first()
    ]
    if never_fetched:
        threads = [threading.Thread(target=_fetch_and_cache_feed, args=(fid,)) for fid in never_fetched]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        db.session.expire_all()  # pick up rows committed by the fetch threads' sessions

    now_utc = datetime.utcnow()
    cutoff_news = now_utc - timedelta(days=2)
    cutoff_blog = now_utc - timedelta(days=7)

    # Resolve feed_type for every feed referenced in this folder
    all_folder_feed_ids_for_type = list(set(target_sources + tag_sources))
    feed_type_map = {f.id: f.feed_type for f in MasterFeed.query.filter(
        MasterFeed.id.in_(all_folder_feed_ids_for_type)).all()}

    def _split_by_type(fids):
        news = [fid for fid in fids if feed_type_map.get(fid) != 'blog']
        blog = [fid for fid in fids if feed_type_map.get(fid) == 'blog']
        return news, blog

    # Rebuild conditions with per-feed-type cutoffs
    timed_conditions = []
    if target_sources:
        ts_news, ts_blog = _split_by_type(target_sources)
        if ts_news:
            timed_conditions.append(and_(ArticleCache.feed_id.in_(ts_news), ArticleCache.published_at >= cutoff_news))
        if ts_blog:
            timed_conditions.append(and_(ArticleCache.feed_id.in_(ts_blog), ArticleCache.published_at >= cutoff_blog))
    if tag_sources and target_tags:
        tgs_news, tgs_blog = _split_by_type(tag_sources)
        if tgs_news:
            timed_conditions.append(and_(ArticleCache.feed_id.in_(tgs_news), ArticleCache.category_tag.in_(target_tags), ArticleCache.published_at >= cutoff_news))
        if tgs_blog:
            timed_conditions.append(and_(ArticleCache.feed_id.in_(tgs_blog), ArticleCache.category_tag.in_(target_tags), ArticleCache.published_at >= cutoff_blog))

    if not timed_conditions:
        return jsonify({"folder": folder.to_dict(), "articles": []})

    articles = ArticleCache.query.filter(
        or_(*timed_conditions)
    ).order_by(ArticleCache.published_at.desc()).limit(500).all()

    now_utc = datetime.utcnow()
    result = []
    for group in _group_articles(articles):
        group_sorted = sorted(group, key=lambda a: a.published_at or datetime.min, reverse=True)
        latest = group_sorted[0]
        hours_since = (now_utc - latest.published_at).total_seconds() / 3600 if latest.published_at else 24
        score = len(group) / (hours_since + 1)
        result.append({
            "score": round(score, 3),
            "main": latest.to_dict(),
            "related": [a.to_dict() for a in group_sorted[1:]]
        })
    result.sort(key=lambda x: x["score"], reverse=True)

    return jsonify({"folder": folder.to_dict(), "stacks": result[:30]})


@app.route('/api/folders/<int:folder_id>/by-tag', methods=['GET'])
def get_folder_articles_by_tag(folder_id):
    """Folder articles grouped by semantic tag, with embedded grouping nested inside."""
    from collections import defaultdict
    _ensure_featured_seeded()
    folder = Folder.query.get_or_404(folder_id)

    target_sources = json.loads(folder.target_sources) if folder.target_sources else []
    tag_sources    = json.loads(folder.tag_sources)    if folder.tag_sources    else []
    target_tags    = json.loads(folder.target_tags)    if folder.target_tags    else []

    all_folder_feed_ids = list(set(target_sources + tag_sources))
    never_fetched = [
        fid for fid in all_folder_feed_ids
        if not ArticleCache.query.filter(ArticleCache.feed_id == fid).first()
    ]
    if never_fetched:
        threads = [threading.Thread(target=_fetch_and_cache_feed, args=(fid,)) for fid in never_fetched]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        db.session.expire_all()

    now_utc = datetime.utcnow()
    cutoff_news = now_utc - timedelta(days=2)
    cutoff_blog = now_utc - timedelta(days=7)

    all_folder_feed_ids_for_type = list(set(target_sources + tag_sources))
    feed_type_map = {f.id: f.feed_type for f in MasterFeed.query.filter(
        MasterFeed.id.in_(all_folder_feed_ids_for_type)).all()}

    def _split_by_type(fids):
        news = [fid for fid in fids if feed_type_map.get(fid) != 'blog']
        blog = [fid for fid in fids if feed_type_map.get(fid) == 'blog']
        return news, blog

    timed_conditions = []
    if target_sources:
        ts_news, ts_blog = _split_by_type(target_sources)
        if ts_news:
            timed_conditions.append(and_(ArticleCache.feed_id.in_(ts_news), ArticleCache.published_at >= cutoff_news))
        if ts_blog:
            timed_conditions.append(and_(ArticleCache.feed_id.in_(ts_blog), ArticleCache.published_at >= cutoff_blog))
    if tag_sources and target_tags:
        tgs_news, tgs_blog = _split_by_type(tag_sources)
        if tgs_news:
            timed_conditions.append(and_(ArticleCache.feed_id.in_(tgs_news), ArticleCache.category_tag.in_(target_tags), ArticleCache.published_at >= cutoff_news))
        if tgs_blog:
            timed_conditions.append(and_(ArticleCache.feed_id.in_(tgs_blog), ArticleCache.category_tag.in_(target_tags), ArticleCache.published_at >= cutoff_blog))

    if not timed_conditions:
        return jsonify([])

    articles = ArticleCache.query.filter(
        or_(*timed_conditions)
    ).order_by(ArticleCache.published_at.desc()).limit(500).all()

    tag_to_articles = defaultdict(list)
    for article in articles:
        if article.category_tag:
            tag_to_articles[article.category_tag].append(article)

    result = []
    for tag, tag_articles in tag_to_articles.items():
        groups = _group_articles(tag_articles)
        stacks = []
        for group in groups:
            group_sorted = sorted(group, key=lambda a: a.published_at or datetime.min, reverse=True)
            latest = group_sorted[0]
            hours_since = (now_utc - latest.published_at).total_seconds() / 3600 if latest.published_at else 24
            score = len(group) / (hours_since + 1)
            stacks.append({
                "score": round(score, 3),
                "main": latest.to_dict(),
                "related": [a.to_dict() for a in group_sorted[1:]]
            })
        stacks.sort(key=lambda x: (len(x['related']), x['score']), reverse=True)
        result.append({
            "tag": tag,
            "count": len(stacks),
            "header_title": stacks[0]['main']['title'] if stacks else '',
            "stacks": stacks
        })

    result.sort(key=lambda x: x['count'], reverse=True)
    return jsonify(result)


if __name__ == '__main__':
    app.run(debug=True, port=5001)
