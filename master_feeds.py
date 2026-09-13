MASTER_FEEDS = [
    # ─────────────────────────────────────────────
    # 最新ニュース (is_featured=True)
    # ─────────────────────────────────────────────
    {"name": "Google News - World",      "rss_url": "https://news.google.com/rss/headlines/section/topic/WORLD",      "category": "World",      "is_featured": True},
    {"name": "Google News - Technology", "rss_url": "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY", "category": "Technology", "is_featured": True},
    {"name": "Google News - Business",   "rss_url": "https://news.google.com/rss/headlines/section/topic/BUSINESS",   "category": "Business",   "is_featured": True},
    {"name": "BBC World News",           "rss_url": "https://feeds.bbci.co.uk/news/world/rss.xml",                    "category": "World",      "is_featured": True},

    # ─────────────────────────────────────────────
    # 国際ニュース
    # ─────────────────────────────────────────────
    {"name": "Al Jazeera English",    "rss_url": "https://www.aljazeera.com/xml/rss/all.xml",            "category": "World", "is_featured": False},
    {"name": "The Guardian - World",  "rss_url": "https://www.theguardian.com/world/rss",                "category": "World", "is_featured": False},
    {"name": "NPR News",              "rss_url": "https://feeds.npr.org/1001/rss.xml",                   "category": "World", "is_featured": False},
    {"name": "DW News",               "rss_url": "https://rss.dw.com/rdf/rss-en-all",                   "category": "World", "is_featured": False},
    {"name": "France 24 - English",   "rss_url": "https://www.france24.com/en/rss",                     "category": "World", "is_featured": False},

    # ─────────────────────────────────────────────
    # 米国ニュース
    # ─────────────────────────────────────────────
    {"name": "The New York Times",    "rss_url": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", "category": "US News", "is_featured": False},
    {"name": "The Washington Post",   "rss_url": "https://feeds.washingtonpost.com/rss/world",               "category": "US News", "is_featured": False},
    {"name": "CNN Top Stories",       "rss_url": "https://news.google.com/rss/search?q=when:24h+site:cnn.com&hl=en-US&gl=US&ceid=US:en", "category": "US News", "is_featured": False},

    # ─────────────────────────────────────────────
    # 日本語メディア
    # ─────────────────────────────────────────────
    {"name": "NHK ニュース",          "rss_url": "https://www.nhk.or.jp/rss/news/cat0.xml",               "category": "Japan", "is_featured": False},
    {"name": "朝日新聞",              "rss_url": "http://rss.asahi.com/rss/asahi/newsheadlines.rdf",       "category": "Japan", "is_featured": False},
    {"name": "毎日新聞",              "rss_url": "https://mainichi.jp/rss/etc/mainichi-flash.rss",         "category": "Japan", "is_featured": False},

    # ─────────────────────────────────────────────
    # テクノロジー
    # ─────────────────────────────────────────────
    {"name": "TechCrunch",            "rss_url": "https://techcrunch.com/feed/",                          "category": "Technology", "is_featured": False},
    {"name": "The Verge",             "rss_url": "https://www.theverge.com/rss/index.xml",                "category": "Technology", "is_featured": False},
    {"name": "Wired",                 "rss_url": "https://www.wired.com/feed/rss",                        "category": "Technology", "is_featured": False},
    {"name": "Ars Technica",          "rss_url": "https://feeds.arstechnica.com/arstechnica/index",       "category": "Technology", "is_featured": False},
    {"name": "MIT Technology Review", "rss_url": "https://www.technologyreview.com/feed/",                "category": "Technology", "is_featured": False, "feed_type": "blog"},
    {"name": "Hacker News - Top",     "rss_url": "https://hnrss.org/frontpage",                           "category": "Technology", "is_featured": False, "feed_type": "blog"},
    {"name": "VentureBeat",           "rss_url": "https://venturebeat.com/feed/",                         "category": "Technology", "is_featured": False},

    # ─────────────────────────────────────────────
    # AI / 機械学習
    # ─────────────────────────────────────────────
    {"name": "Google AI Blog",        "rss_url": "https://blog.google/technology/ai/rss/",                "category": "AI", "is_featured": False, "feed_type": "blog"},
    {"name": "OpenAI Blog",           "rss_url": "https://openai.com/blog/rss.xml",                       "category": "AI", "is_featured": False, "feed_type": "blog"},
    {"name": "Towards Data Science",  "rss_url": "https://towardsdatascience.com/feed",                   "category": "AI", "is_featured": False, "feed_type": "blog"},

    # ─────────────────────────────────────────────
    # ビジネス / 経済
    # ─────────────────────────────────────────────
    {"name": "Bloomberg - Technology", "rss_url": "https://feeds.bloomberg.com/technology/news.rss",     "category": "Business", "is_featured": False},
    {"name": "Financial Times",        "rss_url": "https://www.ft.com/rss/home",                         "category": "Business", "is_featured": False},
    {"name": "Forbes",                 "rss_url": "https://www.forbes.com/business/feed/",               "category": "Business", "is_featured": False},

    # ─────────────────────────────────────────────
    # サイエンス
    # ─────────────────────────────────────────────
    {"name": "Nature News",           "rss_url": "https://www.nature.com/nature.rss",                    "category": "Science", "is_featured": False, "feed_type": "blog"},
    {"name": "Science Daily",         "rss_url": "https://www.sciencedaily.com/rss/all.xml",             "category": "Science", "is_featured": False},
    {"name": "NASA News",             "rss_url": "https://www.nasa.gov/rss/dyn/breaking_news.rss",       "category": "Science", "is_featured": False, "feed_type": "blog"},
    {"name": "New Scientist",         "rss_url": "https://www.newscientist.com/feed/home/",              "category": "Science", "is_featured": False},
    {"name": "BBC Science",           "rss_url": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "category": "Science", "is_featured": False},

    # ─────────────────────────────────────────────
    # スポーツ
    # ─────────────────────────────────────────────
    {"name": "ESPN Top Headlines",    "rss_url": "https://news.google.com/rss/search?q=when:24h+site:espn.com&hl=en-US&gl=US&ceid=US:en", "category": "Sports", "is_featured": False},
    {"name": "BBC Sport",             "rss_url": "https://feeds.bbci.co.uk/sport/rss.xml",               "category": "Sports", "is_featured": False},

    # ─────────────────────────────────────────────
    # エンタメ
    # ─────────────────────────────────────────────
    {"name": "Variety",               "rss_url": "https://variety.com/feed/",                            "category": "Entertainment", "is_featured": False},
    {"name": "The A.V. Club",         "rss_url": "https://www.avclub.com/rss",                           "category": "Entertainment", "is_featured": False},
    {"name": "Engadget",              "rss_url": "https://www.engadget.com/rss.xml",                     "category": "Entertainment", "is_featured": False},

    # ─────────────────────────────────────────────
    # 健康
    # ─────────────────────────────────────────────
    {"name": "Healthline",            "rss_url": "https://www.healthline.com/rss/health-news",           "category": "Health", "is_featured": False},
    {"name": "BBC Health",            "rss_url": "https://feeds.bbci.co.uk/news/health/rss.xml",         "category": "Health", "is_featured": False},

    # ─────────────────────────────────────────────
    # スペイン語メディア
    # ─────────────────────────────────────────────
    {"name": "El Pais",               "rss_url": "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada", "category": "Spain", "is_featured": False},
    {"name": "El Mundo",              "rss_url": "https://e00-elmundo.uecdn.es/rss/portada.xml",                     "category": "Spain", "is_featured": False},
    {"name": "ABC",                   "rss_url": "https://www.abc.es/rss/atom/espana/",                              "category": "Spain", "is_featured": False},
    {"name": "La Vanguardia",         "rss_url": "https://www.lavanguardia.com/rss/home.xml",                        "category": "Spain", "is_featured": False},

    # ─────────────────────────────────────────────
    # 韓国語メディア
    # ─────────────────────────────────────────────
    # max_per_cycle: these two publish 300+ items a day each — over half of everything
    # ingested — and every article costs 3 LLM calls. Neither is featured, and the feed
    # tab only ever shows the newest 10, so a lower per-cycle cap costs little on screen.
    {"name": "Chosun Ilbo",           "rss_url": "https://www.chosun.com/arc/outboundfeeds/rss/?outputType=xml",    "category": "Korea", "is_featured": False, "max_per_cycle": 3},
    {"name": "Donga Ilbo",            "rss_url": "https://rss.donga.com/total.xml",                                 "category": "Korea", "is_featured": False},
    {"name": "Hankyoreh",             "rss_url": "https://www.hani.co.kr/rss/",                                     "category": "Korea", "is_featured": False},
    {"name": "Yonhap News",           "rss_url": "https://www.yna.co.kr/rss/news.xml",                              "category": "Korea", "is_featured": False, "max_per_cycle": 3},

    # ─────────────────────────────────────────────
    # 台湾メディア（繁体字）
    # ─────────────────────────────────────────────
    {"name": "Liberty Times",         "rss_url": "https://news.ltn.com.tw/rss/all.xml",                            "category": "Taiwan", "is_featured": False},
    {"name": "UDN",                   "rss_url": "https://news.google.com/rss/search?q=when:24h+site:udn.com&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", "category": "Taiwan", "is_featured": False},

    # ─────────────────────────────────────────────
    # 中国語メディア（簡体字）
    # ─────────────────────────────────────────────
    {"name": "People Daily",          "rss_url": "https://news.google.com/rss/search?q=when:24h+site:people.com.cn&hl=zh-CN&gl=CN&ceid=CN:zh-Hans", "category": "China", "is_featured": False},
    {"name": "The Paper CN",          "rss_url": "https://news.google.com/rss/search?q=when:24h+site:thepaper.cn&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",   "category": "China", "is_featured": False},
]
