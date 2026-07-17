import json
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()

_MODEL_DISPLAY_NAMES = {
    'google/gemma-3-27b-it':             'Gemma 3 27B',
    'google/gemma-3-12b-it':             'Gemma 3 12B',
    'google/gemma-3-4b-it':              'Gemma 3 4B',
    'google/gemini-2.5-flash':           'Gemini 2.5 Flash',
    'google/gemini-2.5-flash-lite':      'Gemini 2.5 Flash Lite',
    'google/gemini-2.0-flash-lite':      'Gemini 2.0 Flash Lite',
    'meta-llama/llama-3.3-70b-instruct': 'Llama 3.3 70B',
    'meta-llama/llama-3.1-8b-instruct':  'Llama 3.1 8B',
}

def format_model_name(model_id):
    if not model_id:
        return None
    return _MODEL_DISPLAY_NAMES.get(model_id, model_id.split('/')[-1])

class MasterFeed(db.Model):
    """
    Available media sources in the system.
    """
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    rss_url = db.Column(db.String(255), nullable=False, unique=True)
    category = db.Column(db.String(50), nullable=False)
    is_featured = db.Column(db.Boolean, default=False, nullable=False)
    feed_type   = db.Column(db.String(20), default='news', nullable=False, server_default='news')

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "rss_url": self.rss_url,
            "category": self.category,
            "feed_type": self.feed_type,
        }


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id           = db.Column(db.Integer, primary_key=True)
    google_id    = db.Column(db.Text, unique=True, nullable=False)
    email        = db.Column(db.Text, nullable=True)
    display_name = db.Column(db.Text, nullable=True)
    avatar_url   = db.Column(db.Text, nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
        }


class Folder(db.Model):
    """User-defined folders that filter articles by source and/or semantic tag."""
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.Text, nullable=False)
    target_sources = db.Column(db.Text, nullable=True)  # JSON array of MasterFeed IDs (all articles)
    tag_sources  = db.Column(db.Text, nullable=True)    # JSON array of MasterFeed IDs (tag-filtered)
    target_tags  = db.Column(db.Text, nullable=True)    # JSON array of tag strings
    is_favorite  = db.Column(db.Boolean, default=False, nullable=False)
    sort_order   = db.Column(db.Integer, default=0, nullable=False)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    def to_dict(self):
        import json
        return {
            "id": self.id,
            "name": self.name,
            "target_sources": json.loads(self.target_sources) if self.target_sources else [],
            "tag_sources": json.loads(self.tag_sources) if self.tag_sources else [],
            "target_tags": json.loads(self.target_tags) if self.target_tags else [],
            "is_favorite": self.is_favorite,
            "sort_order": self.sort_order,
        }


class ArticleCache(db.Model):
    """
    Cached and AI-analyzed news items.
    """
    id = db.Column(db.Integer, primary_key=True)
    feed_id = db.Column(db.Integer, db.ForeignKey('master_feed.id'), nullable=False)
    guid = db.Column(db.String(255), nullable=False, unique=True)
    title = db.Column(db.String(255), nullable=False)
    link = db.Column(db.String(255), nullable=False)
    summary = db.Column(db.Text, nullable=True)
    ai_insights = db.Column(db.Text, nullable=True)
    category_tag = db.Column(db.String(255), nullable=True)
    topic_tags = db.Column(db.String(255), nullable=True)    # JSON array e.g. ["#OpenAI", "#GPT-5"]
    youtube_query = db.Column(db.String(255), nullable=True)
    youtube_video_id = db.Column(db.String(20), nullable=True)
    published_at = db.Column(db.DateTime, nullable=True)
    thumbnail_url = db.Column(db.String(512), nullable=True)
    source_lang = db.Column(db.String(10), nullable=True)
    insights_lang = db.Column(db.String(10), nullable=True)
    summary_translated = db.Column(db.Text, nullable=True)
    insights_translated = db.Column(db.Text, nullable=True)
    translated_lang = db.Column(db.String(10), nullable=True)
    translated_engine = db.Column(db.String(20), nullable=True)
    embedding = db.Column(db.Text, nullable=True)  # JSON-encoded normalized float vector
    insights_model = db.Column(db.Text, nullable=True)
    ai_insights_ja = db.Column(db.Text, nullable=True)

    feed = db.relationship('MasterFeed')
    translations = db.relationship('ArticleTranslation', back_populates='article',
                                   cascade='all, delete-orphan', lazy='dynamic')

    def _parse_topic_tags(self):
        if not self.topic_tags:
            return []
        try:
            return json.loads(self.topic_tags)
        except (ValueError, TypeError):
            return [t.strip() for t in self.topic_tags.split(',') if t.strip()]

    def to_dict(self):
        return {
            "id": self.id,
            "feed_id": self.feed_id,
            "guid": self.guid,
            "title": self.title,
            "link": self.link,
            "summary": self.summary,
            "ai_insights": self.ai_insights,
            "ai_insights_ja": self.ai_insights_ja,
            "category_tag": self.category_tag,  # null = tagging still pending (frontend shows "Analyzing…")
            "topic_tags": self._parse_topic_tags(),
            "youtube_query": self.youtube_query,
            "published_at": self.published_at.isoformat() + 'Z' if self.published_at else None,
            "thumbnail_url": self.thumbnail_url,
            "insights_lang": self.insights_lang,
            "insights_model": format_model_name(self.insights_model),
            "source": self.feed.name if self.feed else "",
            "read": False # Default for UI
        }


class ArticleTranslation(db.Model):
    """Per-article, per-language translation cache."""
    __tablename__ = 'article_translation'
    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey('article_cache.id', ondelete='CASCADE'), nullable=False)
    lang = db.Column(db.String(10), nullable=False)
    title = db.Column(db.String(255), nullable=True)
    summary = db.Column(db.Text, nullable=True)
    insights = db.Column(db.Text, nullable=True)
    engine = db.Column(db.String(20), nullable=True)
    translated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('article_id', 'lang', name='uq_article_lang'),)

    article = db.relationship('ArticleCache', back_populates='translations')
