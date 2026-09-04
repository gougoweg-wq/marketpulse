"""Каталог источников: 300+ лент.

Состав:
  1. Статический список крупных финансовых и техно-изданий (RSS).
  2. Генерируемые ленты по каждому тикеру вотчлиста
     (Yahoo Finance, Google News, Nasdaq, HackerNews).
  3. Сабреддиты про рынок (через RSS реддита).
  4. Публичные телеграм-каналы (через веб-превью t.me/s/).

Часть лент со временем умирает — это норма. Сборщик считает
error_streak и отключает источник после серии неудач.
"""
from __future__ import annotations

from dataclasses import dataclass

from marketpulse.config import settings
from marketpulse.db.models import SourceKind


@dataclass(frozen=True)
class FeedSpec:
    kind: SourceKind
    name: str
    url: str
    category: str = "general"
    weight: float = 1.0


# Имя компании для поисковых лент — точнее, чем голый тикер
TICKER_QUERY = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "GOOGL": "Google",
    "AMZN": "Amazon", "META": "Meta Platforms", "TSLA": "Tesla", "AMD": "AMD",
    "INTC": "Intel", "NFLX": "Netflix", "JPM": "JPMorgan", "GS": "Goldman Sachs",
    "XOM": "Exxon", "CVX": "Chevron", "KO": "Coca-Cola", "PFE": "Pfizer",
    "JNJ": "Johnson Johnson", "BA": "Boeing", "DIS": "Disney", "V": "Visa",
    "MA": "Mastercard", "PYPL": "PayPal", "COIN": "Coinbase", "GLD": "gold price",
    "SLV": "silver price", "USO": "oil price", "SPY": "S&P 500",
    "QQQ": "Nasdaq 100", "IWM": "Russell 2000", "TLT": "treasury bonds",
}

STATIC_RSS: list[tuple[str, str, str, float]] = [
    # (name, url, category, weight)
    ("CNBC Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html", "markets", 1.5),
    ("CNBC Markets", "https://www.cnbc.com/id/20910258/device/rss/rss.html", "markets", 1.5),
    ("CNBC Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html", "macro", 1.3),
    ("CNBC Tech", "https://www.cnbc.com/id/19854910/device/rss/rss.html", "tech", 1.2),
    ("CNBC Finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html", "markets", 1.3),
    ("MarketWatch Top", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "markets", 1.5),
    ("MarketWatch Pulse", "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines", "markets", 1.4),
    ("MarketWatch Market Pulse", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse", "markets", 1.4),
    ("WSJ Markets", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", "markets", 1.6),
    ("WSJ US Business", "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness", "business", 1.5),
    ("WSJ World News", "https://feeds.content.dowjones.io/public/rss/RSSWorldNews", "macro", 1.3),
    ("WSJ Tech", "https://feeds.content.dowjones.io/public/rss/RSSWSJD", "tech", 1.3),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex", "markets", 1.3),
    ("Investing.com News", "https://www.investing.com/rss/news.rss", "markets", 1.2),
    ("Investing.com Stock", "https://www.investing.com/rss/news_25.rss", "markets", 1.2),
    ("Investing.com Economy", "https://www.investing.com/rss/news_14.rss", "macro", 1.1),
    ("Investing.com Commodities", "https://www.investing.com/rss/news_11.rss", "commodities", 1.1),
    ("Investing.com Forex", "https://www.investing.com/rss/news_1.rss", "forex", 1.0),
    ("Business Insider Markets", "https://markets.businessinsider.com/rss/news", "markets", 1.1),
    ("Fortune", "https://fortune.com/feed/", "business", 1.0),
    ("Forbes Money", "https://www.forbes.com/money/feed/", "business", 1.0),
    ("Forbes Business", "https://www.forbes.com/business/feed/", "business", 1.0),
    ("Financial Times", "https://www.ft.com/rss/home", "markets", 1.5),
    ("The Economist Finance", "https://www.economist.com/finance-and-economics/rss.xml", "macro", 1.3),
    ("Guardian Business", "https://www.theguardian.com/uk/business/rss", "business", 1.0),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml", "business", 1.1),
    ("NYT Business", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", "business", 1.2),
    ("NYT Economy", "https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml", "macro", 1.2),
    ("NYT Technology", "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml", "tech", 1.1),
    ("Seeking Alpha Market News", "https://seekingalpha.com/market_currents.xml", "markets", 1.3),
    ("Zacks Press Releases", "https://www.zacks.com/rss/rss_news_category.php?cat=press", "markets", 0.9),
    ("Benzinga", "https://www.benzinga.com/feed", "markets", 1.0),
    ("StockTwits Blog", "https://stocktwits.com/blog/feed/", "social", 0.8),
    ("Motley Fool", "https://www.fool.com/feeds/index.aspx", "markets", 0.9),
    ("Barchart News", "https://www.barchart.com/news/rss", "markets", 0.9),
    ("TalkMarkets", "https://talkmarkets.com/rss", "markets", 0.7),
    ("ETF Trends", "https://www.etftrends.com/feed/", "markets", 0.8),
    # --- макро / регуляторы (важные первоисточники) ---
    ("Fed Press Releases", "https://www.federalreserve.gov/feeds/press_all.xml", "macro", 2.0),
    ("SEC Press Releases", "https://www.sec.gov/news/pressreleases.rss", "macro", 1.8),
    ("SEC Litigation", "https://www.sec.gov/rss/litigation/litreleases.xml", "macro", 1.5),
    ("BLS Latest", "https://www.bls.gov/feed/news_release.rss", "macro", 1.8),
    ("Treasury Press", "https://home.treasury.gov/rss/press.xml", "macro", 1.6),
    ("ECB Press", "https://www.ecb.europa.eu/rss/press.html", "macro", 1.5),
    ("IMF News", "https://www.imf.org/en/News/RSS?Language=ENG", "macro", 1.3),
    # --- техно (двигают техгигантов) ---
    ("TechCrunch", "https://techcrunch.com/feed/", "tech", 1.1),
    ("The Verge", "https://www.theverge.com/rss/index.xml", "tech", 1.0),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "tech", 1.0),
    ("Wired Business", "https://www.wired.com/feed/category/business/latest/rss", "tech", 1.0),
    ("Engadget", "https://www.engadget.com/rss.xml", "tech", 0.9),
    ("VentureBeat", "https://venturebeat.com/feed/", "tech", 0.9),
    ("ZDNet", "https://www.zdnet.com/news/rss.xml", "tech", 0.8),
    ("9to5Mac", "https://9to5mac.com/feed/", "tech", 0.9),
    ("MacRumors", "https://feeds.macrumors.com/MacRumors-All", "tech", 0.9),
    ("Android Authority", "https://www.androidauthority.com/feed/", "tech", 0.7),
    ("The Register", "https://www.theregister.com/headlines.atom", "tech", 0.8),
    ("AnandTech", "https://www.anandtech.com/rss/", "tech", 0.8),
    ("Tom's Hardware", "https://www.tomshardware.com/feeds/all", "tech", 0.8),
    # --- крипта (влияет на COIN и настроение риска) ---
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "crypto", 1.1),
    ("Cointelegraph", "https://cointelegraph.com/rss", "crypto", 1.0),
    ("Bitcoin Magazine", "https://bitcoinmagazine.com/feed", "crypto", 0.8),
    ("Decrypt", "https://decrypt.co/feed", "crypto", 0.9),
    ("The Block", "https://www.theblock.co/rss.xml", "crypto", 1.0),
    # --- энергия / сырьё ---
    ("OilPrice.com", "https://oilprice.com/rss/main", "commodities", 1.0),
    ("Kitco News", "https://www.kitco.com/rss/KitcoNews.xml", "commodities", 1.0),
    ("Mining.com", "https://www.mining.com/feed/", "commodities", 0.8),
]

SUBREDDITS: list[tuple[str, float]] = [
    ("stocks", 1.2), ("investing", 1.2), ("wallstreetbets", 1.5),
    ("StockMarket", 1.0), ("options", 0.9), ("SecurityAnalysis", 0.9),
    ("ValueInvesting", 0.9), ("Daytrading", 0.8), ("pennystocks", 0.6),
    ("CryptoCurrency", 0.9), ("Bitcoin", 0.8), ("economy", 1.0),
    ("Economics", 1.0), ("finance", 1.0), ("algotrading", 0.8),
]

# Публичные телеграм-каналы (читаются через t.me/s/<name> без API).
# Вес ниже — источники шумные; именно их экстремумы питают контрарианский модуль.
TELEGRAM_CHANNELS: list[tuple[str, str, float]] = [
    ("markettwits", "social", 1.2),
    ("bloomberg", "markets", 1.0),
    ("financelist", "markets", 0.8),
    ("wallstreetpro", "social", 0.8),
    ("cbonds_global", "markets", 0.9),
    ("banksta", "social", 0.8),
    ("economika", "macro", 0.8),
    ("finpol", "macro", 0.7),
    ("marketsnapshot", "markets", 0.8),
    ("stock_charts", "social", 0.7),
    ("forbesrussia", "business", 0.9),
    ("rbc_news", "business", 0.9),
    ("kommersant", "business", 0.9),
    ("vedomosti", "business", 0.9),
    ("bitkogan", "markets", 1.0),
    ("themovchans", "markets", 0.9),
    ("cryptodaily", "crypto", 0.7),
    ("if_stocks", "markets", 0.9),
    ("investorbiz", "social", 0.7),
    ("profinansy", "social", 0.7),
]


def build_catalog() -> list[FeedSpec]:
    specs: list[FeedSpec] = []

    for name, url, cat, w in STATIC_RSS:
        specs.append(FeedSpec(SourceKind.rss, name, url, cat, w))

    # --- персональные ленты на каждый тикер ---
    for sym in settings.watchlist:
        query = TICKER_QUERY.get(sym, sym)
        q = query.replace(" ", "+")
        specs.append(FeedSpec(
            SourceKind.rss, f"Yahoo {sym}",
            f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US",
            "ticker", 1.2,
        ))
        specs.append(FeedSpec(
            SourceKind.rss, f"GoogleNews {sym}",
            f"https://news.google.com/rss/search?q={q}+stock&hl=en-US&gl=US&ceid=US:en",
            "ticker", 1.0,
        ))
        specs.append(FeedSpec(
            SourceKind.rss, f"Nasdaq {sym}",
            f"https://www.nasdaq.com/feed/rssoutbound?symbol={sym}",
            "ticker", 1.0,
        ))
        specs.append(FeedSpec(
            SourceKind.rss, f"HN {sym}",
            f"https://hnrss.org/newest?q={q}",
            "tech-social", 0.7,
        ))

    for sub, w in SUBREDDITS:
        specs.append(FeedSpec(
            SourceKind.reddit, f"r/{sub}",
            f"https://www.reddit.com/r/{sub}/.rss",
            "social", w,
        ))

    if settings.telegram_enabled:
        for chan, cat, w in TELEGRAM_CHANNELS:
            specs.append(FeedSpec(
                SourceKind.telegram, f"tg:{chan}",
                f"https://t.me/s/{chan}",
                cat, w,
            ))

    # защита от дублей URL
    seen: set[str] = set()
    unique = []
    for s in specs:
        if s.url not in seen:
            seen.add(s.url)
            unique.append(s)
    return unique


def seed_sources() -> int:
    """Заливает каталог в базу. Возвращает число источников."""
    from sqlalchemy import select

    from marketpulse.db.models import Source
    from marketpulse.db.session import db_session

    specs = build_catalog()
    with db_session() as s:
        existing = set(s.execute(select(Source.url)).scalars())
        for spec in specs:
            if spec.url not in existing:
                s.add(Source(
                    kind=spec.kind, name=spec.name, url=spec.url,
                    category=spec.category, weight=spec.weight,
                ))
    return len(specs)
