#!/usr/bin/env python3
"""
Financial / global-macro news aggregator for the morning brief.

Pulls three tiers of source in one concurrent network pass:

  1. TOPIC QUERIES   -- 25 topics x 3 Google News RSS queries (75 queries),
                        covering the brief's original 13 research topics plus
                        global-macro regions and the playbook's structural
                        themes (fiscal dominance, private credit, AI capex
                        financing, deglobalization, market structure).
  2. PUBLISHER FEEDS -- ~30 direct RSS feeds from newsrooms (CNBC, MarketWatch,
                        Investing.com, Yahoo Finance, OilPrice, Seeking Alpha)
                        and primary institutions (Fed x4, ECB, BoE, BoJ,
                        Treasury, BLS, EIA x2, Census).
  3. PREMIUM DESKS   -- Google News `site:` queries against the paywalled
                        outlets this tool is meant to replace (FT, WSJ,
                        Economist, Reuters, Bloomberg, Barron's, Nikkei).
                        Headlines only -- per the brief's rules these are
                        signals to confirm elsewhere, not citable facts.

One fetch, two outputs: the widest (weekly) window is pulled once, then the
daily file is derived as a filtered subset -- no second network pass.

Usage:
    python3 fetch_news.py [YYYY-MM-DD] [--daily-hours 36] [--weekly-hours 168]
                          [--per-source 12] [--workers 12] [--timeout 12]

Output:
    briefs/data/<date>-news.json           -- daily window (default 36h)
    briefs/data/<date>-news-weekly.json    -- weekly window (default 168h)

No API key required. Every feed is public RSS. A failure on any single feed is
non-fatal and recorded in the output's `fetch_errors` ledger rather than
aborting the run.
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, Optional

try:
    from zoneinfo import ZoneInfo

    PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover - fallback when tzdata is unavailable
    PACIFIC = timezone(timedelta(hours=-7), name="PT")

try:
    import certifi

    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover - certifi is present in this env
    SSL_CTX = ssl.create_default_context()

ROOT = Path(__file__).resolve().parents[1]  # briefs/
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

USER_AGENT = "Mozilla/5.0 (compatible; MorningBriefBot/1.0; +local-research-tool)"
HEADERS = {"User-Agent": USER_AGENT}

ATOM_NS = "{http://www.w3.org/2005/Atom}"


# ---------------------------------------------------------------------------
# Source catalogue
# ---------------------------------------------------------------------------

# Topic -> (display label, [search queries]). Three queries per topic: one
# broad, one specific, one angled at named analysis/reaction (the brief's
# signal-preservation rule wants named people, not "analysts say").
TOPIC_QUERIES: dict[str, tuple[str, list[str]]] = {
    "top_story": ("Top market story", [
        "stock market today biggest story",
        "markets news today",
        "Wall Street strategist market outlook",
    ]),
    "asia_europe_futures": ("Asia, Europe & futures", [
        "Asia Europe stock market close",
        "US stock futures today",
        "global equities session wrap",
    ]),
    "treasury_yields": ("Treasury yields & curve", [
        "Treasury yields 10 year 30 year",
        "yield curve steepening term premium",
        "bond strategist yields forecast",
    ]),
    "treasury_auctions": ("Treasury auctions", [
        "Treasury auction results bid to cover",
        "Treasury note bond auction demand tail",
        "primary dealers auction takedown",
    ]),
    "fed_speakers": ("Federal Reserve", [
        "Federal Reserve speech rate hike",
        "FOMC rate odds FedWatch",
        "Fed official inflation comments",
    ]),
    "central_banks": ("Global central banks", [
        "ECB interest rate decision",
        "Bank of England Bank of Japan rates",
        "central bank policy divergence",
    ]),
    "econ_data": ("US economic data", [
        "US economic data release today",
        "economic calendar this week",
        "inflation CPI PCE report",
    ]),
    "labor_market": ("Labor market", [
        "jobless claims payrolls report",
        "US labor market hiring layoffs",
        "wage growth JOLTS quits",
    ]),
    "oil_geopolitics": ("Oil & geopolitics", [
        "oil price geopolitics",
        "Strait of Hormuz Middle East oil",
        "OPEC supply crude outlook",
    ]),
    "metals": ("Gold, copper & metals", [
        "gold price today",
        "copper price today",
        "precious metals industrial metals outlook",
    ]),
    "energy_power": ("Energy & power", [
        "natural gas prices electricity demand",
        "uranium nuclear power data center energy",
        "energy transition investment",
    ]),
    "fx": ("FX & dollar", [
        "dollar index DXY today",
        "USDJPY yen intervention",
        "emerging market currency pressure",
    ]),
    "credit": ("Credit markets", [
        "high yield credit spreads",
        "investment grade bond issuance",
        "corporate credit default risk",
    ]),
    "private_credit": ("Private credit & BDCs", [
        "private credit direct lending",
        "BDC business development company NAV",
        "CLO collateralized loan obligation market",
    ]),
    "ai_capex_financing": ("AI capex & financing", [
        "AI data center financing debt",
        "hyperscaler capital expenditure AI",
        "AI infrastructure credit risk",
    ]),
    "vix_funding": ("Volatility & funding", [
        "VIX volatility market today",
        "repo market funding stress SOFR",
        "options gamma 0DTE market structure",
    ]),
    "market_structure": ("Market structure & flows", [
        "passive index fund flows concentration",
        "market liquidity depth deterioration",
        "hedge fund positioning basis trade",
    ]),
    "deficit_issuance": ("Deficit & issuance", [
        "US deficit Treasury issuance",
        "Treasury quarterly refunding",
        "sovereign debt fiscal sustainability",
    ]),
    "banks_financials": ("Banks & financials", [
        "bank earnings deposits lending",
        "bank regulation capital requirements",
        "regional banks commercial real estate",
    ]),
    "equities_earnings": ("Equities & earnings", [
        "earnings season guidance results",
        "megacap tech stocks valuation",
        "equity strategist S&P 500 target",
    ]),
    "china_macro": ("China macro", [
        "China economy GDP stimulus",
        "PBOC policy yuan",
        "China property developers debt",
    ]),
    "japan_macro": ("Japan macro", [
        "Bank of Japan yen JGB yields",
        "Japan inflation wages economy",
        "Japan equities Nikkei outlook",
    ]),
    "europe_macro": ("Europe macro", [
        "eurozone economy inflation growth",
        "Germany economy industrial output",
        "European bond spreads periphery",
    ]),
    "uk_macro": ("UK macro", [
        "UK economy inflation gilts",
        "Bank of England policy outlook",
        "UK fiscal budget borrowing",
    ]),
    "em_macro": ("Emerging markets", [
        "emerging markets capital flows",
        "IMF emerging market debt",
        "India Brazil Mexico economy markets",
    ]),
    "trade_tariffs": ("Trade & tariffs", [
        "tariffs trade war supply chain",
        "export controls semiconductors",
        "reshoring industrial policy",
    ]),
}

# Publisher / primary-institution RSS feeds. `tier` drives how the renderer
# groups them: "wire" = newsroom, "primary" = the institution itself.
PUBLISHER_FEEDS: dict[str, tuple[str, str, str]] = {
    # key: (display label, url, tier)
    "cnbc_top": ("CNBC -- Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html", "wire"),
    "cnbc_economy": ("CNBC -- Economy", "https://www.cnbc.com/id/20910258/device/rss/rss.html", "wire"),
    "cnbc_finance": ("CNBC -- Finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html", "wire"),
    "cnbc_world": ("CNBC -- World", "https://www.cnbc.com/id/100727362/device/rss/rss.html", "wire"),
    "cnbc_markets": ("CNBC -- Markets", "https://www.cnbc.com/id/20409666/device/rss/rss.html", "wire"),
    "mw_top": ("MarketWatch -- Top Stories", "http://feeds.marketwatch.com/marketwatch/topstories/", "wire"),
    "mw_pulse": ("MarketWatch -- Market Pulse", "http://feeds.marketwatch.com/marketwatch/marketpulse/", "wire"),
    "mw_realtime": ("MarketWatch -- Real-time", "http://feeds.marketwatch.com/marketwatch/realtimeheadlines/", "wire"),
    "yahoo_finance": ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex", "wire"),
    "investing_news": ("Investing.com -- News", "https://www.investing.com/rss/news.rss", "wire"),
    "investing_economy": ("Investing.com -- Economy", "https://www.investing.com/rss/news_14.rss", "wire"),
    "investing_fx": ("Investing.com -- Forex", "https://www.investing.com/rss/news_1.rss", "wire"),
    "investing_commodities": ("Investing.com -- Commodities", "https://www.investing.com/rss/news_11.rss", "wire"),
    "oilprice": ("OilPrice.com", "https://oilprice.com/rss/main", "wire"),
    "fed_press": ("Federal Reserve -- Press", "https://www.federalreserve.gov/feeds/press_all.xml", "primary"),
    "fed_monetary": ("Federal Reserve -- Monetary Policy", "https://www.federalreserve.gov/feeds/press_monetary.xml", "primary"),
    "fed_speeches": ("Federal Reserve -- Speeches", "https://www.federalreserve.gov/feeds/speeches.xml", "primary"),
    "fed_testimony": ("Federal Reserve -- Testimony", "https://www.federalreserve.gov/feeds/testimony.xml", "primary"),
    "ecb": ("European Central Bank", "https://www.ecb.europa.eu/rss/press.html", "primary"),
    "boe": ("Bank of England", "https://www.bankofengland.co.uk/rss/news", "primary"),
    "boj": ("Bank of Japan", "https://www.boj.or.jp/en/rss/whatsnew.xml", "primary"),
    "treasury": ("US Treasury", "https://home.treasury.gov/rss.xml", "primary"),
    "bls": ("Bureau of Labor Statistics", "https://www.bls.gov/feed/bls_latest.rss", "primary"),
    "census": ("Census -- Economic Indicators", "https://www.census.gov/economic-indicators/indicator.xml", "primary"),
    "eia_today": ("EIA -- Today in Energy", "https://www.eia.gov/rss/todayinenergy.xml", "primary"),
    "eia_press": ("EIA -- Press", "https://www.eia.gov/rss/press_rss.xml", "primary"),
}

# The paywalled desks this tool is meant to stand in for. Google News surfaces
# their headlines without the paywall; the article bodies stay behind it, so
# these are reading signals -- what those desks are leading with -- not
# citable facts. The brief's rules already say to confirm them elsewhere.
PREMIUM_SITES: dict[str, tuple[str, str]] = {
    # key: (display label, google-news site: query)
    "ft": ("Financial Times", "site:ft.com"),
    "wsj": ("Wall Street Journal", "site:wsj.com"),
    "economist": ("The Economist", "site:economist.com"),
    "reuters": ("Reuters", "site:reuters.com markets OR economy"),
    "bloomberg": ("Bloomberg", "site:bloomberg.com markets OR economy"),
    "barrons": ("Barron's", "site:barrons.com"),
    "nikkei": ("Nikkei Asia", "site:asia.nikkei.com economy OR markets"),
    "imf_bis": ("IMF / BIS / OECD", "site:imf.org OR site:bis.org OR site:oecd.org economy"),
}


# Broad newsroom feeds carry two kinds of filler that crowd out macro news:
# single-name corporate-action boilerplate (dividend declarations, 52-week
# prints, buyback notices) and first-person personal-finance advice columns.
# Both are legitimate output for those desks and useless here, so they are
# dropped at ingest rather than left for the reader to scroll past.
NOISE_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bdeclares\b.*\b(dividend|distribution)\b",
        r"\bdividend\b.*\bdeclare",
        r"\b(raises|increases|cuts|maintains)\b.*\bdividend\b.*\bto \$?\d",
        r"\bstock (hits|touches|sets)\b.*\b(52[- ]week|all[- ]time)\b",
        r"\b52[- ]week (low|high)\b.*\bat \$?\d",
        r"\boptions flow shows\b",
        r"\bput/call ratio\b",
        r"\b(announces|completes)\b.*\b(pricing of|offering of)\b.*\bnotes\b",
        r"\bearnings call transcript\b",
        r"\bprice target (raised|lowered|cut) to\b",
        r"\binitiates coverage\b",
        # Advice-column voice: first-person money/relationship questions.
        r"^(i|my|we)[' ].{0,80}\b(husband|wife|boyfriend|girlfriend|dating|mother|father|son|daughter|inherit)",
        r"\bshould i\b.*\?$",
        r"\bmy finances\b",
        r"\bam i (being|wrong)\b",
        r"\bdear (quentin|moneyist)\b",
        # Section index pages that syndicate as if they were articles.
        r"^print edition\b",
        r"^today'?s paper\b",
        r"^(news|markets|opinion) \| ",
    )
)


def is_noise(title: str) -> bool:
    """True when a headline is single-name corporate-action boilerplate, an
    advice column, or a section index page rather than market/macro news."""
    text = title.strip()
    return any(pattern.search(text) for pattern in NOISE_PATTERNS)


def _outlet_matches(candidate: str, source: str) -> bool:
    """Loose match between a title's trailing segment and the item's source:
    'barrons.com' vs "Barron's", 'WSJ' vs 'Wall Street Journal' abbreviations."""
    left = normalize_title(candidate).replace(" ", "")
    right = normalize_title(source).replace(" ", "")
    if not left or not right:
        return False
    return left == right or left.startswith(right) or right.startswith(left)


def strip_outlet_suffix(title: str, source: Optional[str]) -> tuple[str, Optional[str]]:
    """Google News renders titles as 'Headline - Outlet'. Peel the outlet off
    so it shows once, in the source slot, instead of twice.

    Only strips when the trailing segment actually looks like the outlet --
    matching the item's own <source>, or domain/title-cased when no source was
    given. A headline that merely contains ' - ' keeps its tail intact."""
    if " - " not in title:
        return title, source
    head, _, tail = title.rpartition(" - ")
    head, tail = head.strip(), tail.strip()
    if not head or not (2 <= len(tail) <= 40):
        return title, source
    if source:
        return (head, source) if _outlet_matches(tail, source) else (title, source)
    if "." in tail or tail.istitle() or tail.isupper():
        return head, tail
    return title, source


def is_redundant_summary(summary: str, title: str) -> bool:
    """Google News descriptions are frequently the headline again, sometimes
    with the outlet appended. Showing that under the headline is pure noise."""
    normalized_summary = normalize_title(summary)
    normalized_title = normalize_title(title)
    if not normalized_summary or not normalized_title:
        return True
    return (
        normalized_summary == normalized_title
        or normalized_summary.startswith(normalized_title)
        or normalized_title.startswith(normalized_summary)
    )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Headline:
    """One news item, normalized across every feed shape we ingest."""

    title: str
    link: Optional[str]
    source: Optional[str]
    published_utc: Optional[str]
    published_pt: Optional[str]
    age_hours: Optional[float]
    age_label: str
    summary: Optional[str]
    origin: str
    tier: str
    group: str
    group_label: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class FetchResult:
    """Outcome of one feed/query pull -- headlines plus any error to report."""

    group: str
    group_label: str
    tier: str
    origin: str
    headlines: list[Headline] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------


def _fetch_bytes(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout, context=SSL_CTX) as response:
        return response.read()


def strip_html(raw: str, limit: int = 400) -> str:
    """RSS <description> is frequently escaped HTML; reduce it to flat text."""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def normalize_title(title: str) -> str:
    """Dedup key: casefolded, punctuation-stripped, whitespace-collapsed.

    Wire stories get rewritten headline-to-headline across outlets, so exact
    string matching under-dedupes badly; this catches the common case of the
    same headline with different punctuation or an outlet suffix."""
    text = title.casefold()
    text = re.sub(r"[^a-z0-9 ]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def humanize_age(hours: Optional[float]) -> str:
    """'3h ago' / '2d ago' -- the scannable form for a headline list."""
    if hours is None:
        return "undated"
    if hours < 1:
        minutes = max(1, int(round(hours * 60)))
        return f"{minutes}m ago"
    if hours < 24:
        return f"{int(round(hours))}h ago"
    days = hours / 24
    if days < 7:
        return f"{int(round(days))}d ago"
    return f"{int(round(days / 7))}w ago"


def _parse_datetime(raw: Optional[str]) -> Optional[datetime]:
    """RSS pubDate (RFC 822) or Atom updated (ISO 8601), whichever a feed uses."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _element_text(element: ET.Element, *tags: str) -> Optional[str]:
    for tag in tags:
        found = element.find(tag)
        if found is not None and found.text and found.text.strip():
            return found.text.strip()
    return None


def _build_headline(
    element: ET.Element,
    now: datetime,
    group: str,
    group_label: str,
    tier: str,
    origin: str,
    fallback_source: Optional[str],
) -> Optional[Headline]:
    title = _element_text(element, "title", f"{ATOM_NS}title")
    if not title:
        return None

    link = _element_text(element, "link", "guid")
    if not link:
        link_el = element.find(f"{ATOM_NS}link")
        link = link_el.get("href") if link_el is not None else None

    # The item's own <source> only -- the feed label is applied *after* the
    # outlet-suffix strip, so a feed that omits <source> can still have the
    # outlet recovered from its title instead of being masked by the label.
    item_source = _element_text(element, "source")
    published = _parse_datetime(
        _element_text(element, "pubDate", "published", f"{ATOM_NS}updated", f"{ATOM_NS}published", "date")
    )
    summary_raw = _element_text(element, "description", "summary", f"{ATOM_NS}summary", f"{ATOM_NS}content")

    title, detected_source = strip_outlet_suffix(title, item_source)
    source = detected_source or fallback_source
    summary = strip_html(summary_raw) if summary_raw else None
    if summary and is_redundant_summary(summary, title):
        summary = None

    age_hours = (now - published).total_seconds() / 3600 if published else None
    return Headline(
        title=title,
        link=link,
        source=source,
        published_utc=published.isoformat() if published else None,
        published_pt=published.astimezone(PACIFIC).strftime("%a %b %-d, %-I:%M %p PT") if published else None,
        age_hours=round(age_hours, 2) if age_hours is not None else None,
        age_label=humanize_age(age_hours),
        summary=summary,
        origin=origin,
        tier=tier,
        group=group,
        group_label=group_label,
    )


def parse_feed(
    raw: bytes,
    now: datetime,
    group: str,
    group_label: str,
    tier: str,
    origin: str,
    limit: int,
    fallback_source: Optional[str] = None,
) -> list[Headline]:
    """Parse an RSS 2.0 or Atom document into Headlines, newest-first order
    preserved as the feed gave it (feeds are already reverse-chronological)."""
    root = ET.fromstring(raw)
    items = root.findall("./channel/item") or root.findall(f".//{ATOM_NS}entry")
    headlines = []
    # Filter noise *before* applying the limit, so a feed that opens with ten
    # dividend notices still contributes its real stories.
    for element in items:
        if len(headlines) >= limit:
            break
        headline = _build_headline(element, now, group, group_label, tier, origin, fallback_source)
        if headline is None or is_noise(headline.title):
            continue
        headlines.append(headline)
    return headlines


def google_news_url(query: str) -> str:
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )


def fetch_one(
    url: str,
    group: str,
    group_label: str,
    tier: str,
    origin: str,
    limit: int,
    timeout: int,
    now: datetime,
    fallback_source: Optional[str] = None,
) -> FetchResult:
    """Fetch + parse a single feed. Never raises: failures land in .error so a
    dead feed degrades this run's coverage instead of ending it."""
    result = FetchResult(group=group, group_label=group_label, tier=tier, origin=origin)
    try:
        raw = _fetch_bytes(url, timeout)
    except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    try:
        result.headlines = parse_feed(raw, now, group, group_label, tier, origin, limit, fallback_source)
    except ET.ParseError as exc:
        result.error = f"ParseError: {exc}"
    return result


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def dedupe(headlines: Iterable[Headline]) -> list[Headline]:
    """Drop repeat headlines, keeping the first (feeds arrive newest-first)."""
    seen: set[str] = set()
    unique: list[Headline] = []
    for headline in headlines:
        key = normalize_title(headline.title)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(headline)
    return unique


def sort_newest_first(headlines: list[Headline]) -> list[Headline]:
    """Undated items sort last rather than being treated as infinitely old."""
    return sorted(
        headlines,
        key=lambda h: (h.age_hours is None, h.age_hours if h.age_hours is not None else 0.0),
    )


def within_window(headline: Headline, max_age_hours: float) -> bool:
    """Undated items are kept: Google News omits pubDate on some aggregated
    entries, and silently dropping real headlines is worse than passing an
    unknown date through for the narrative step to judge."""
    if headline.age_hours is None:
        return True
    return headline.age_hours <= max_age_hours


def build_rollup(groups: dict[str, dict], now: datetime, window_hours: int) -> dict:
    """Cross-cutting summaries: volume by day, busiest topics, top outlets.

    These are what make the weekly view readable as a *week* rather than a
    long daily list -- which day the news clustered on, who covered it most."""
    all_headlines = [h for group in groups.values() for h in group["headlines"]]

    by_day: dict[str, list] = defaultdict(list)
    for headline in all_headlines:
        if not headline.get("published_utc"):
            continue
        published = datetime.fromisoformat(headline["published_utc"]).astimezone(PACIFIC)
        by_day[published.strftime("%Y-%m-%d")].append(headline)

    day_buckets = []
    for day in sorted(by_day, reverse=True):
        items = sort_dicts_newest_first(by_day[day])
        day_buckets.append({
            "date": day,
            "label": datetime.strptime(day, "%Y-%m-%d").strftime("%A, %b %-d"),
            "count": len(items),
            "top_sources": [s for s, _ in Counter(
                i["source"] for i in items if i.get("source")
            ).most_common(5)],
            "headlines": items,
        })

    source_counts = Counter(h["source"] for h in all_headlines if h.get("source"))
    topic_volume = sorted(
        (
            {"group": key, "label": group["label"], "count": len(group["headlines"])}
            for key, group in groups.items()
        ),
        key=lambda row: row["count"],
        reverse=True,
    )

    return {
        "window_hours": window_hours,
        "total_headlines": len(all_headlines),
        "distinct_sources": len(source_counts),
        "by_day": day_buckets,
        "top_sources": [{"source": s, "count": c} for s, c in source_counts.most_common(25)],
        "topic_volume": topic_volume,
        "busiest_day": day_buckets[0]["label"] if day_buckets else None,
    }


def sort_dicts_newest_first(headlines: list[dict]) -> list[dict]:
    return sorted(
        headlines,
        key=lambda h: (h.get("age_hours") is None, h.get("age_hours") or 0.0),
    )


def assemble_view(
    results_by_group: dict[str, list[Headline]],
    catalogue: dict[str, dict],
    max_age_hours: float,
) -> dict[str, dict]:
    """Filter one window out of the single wide pull, per group."""
    view: dict[str, dict] = {}
    for key, meta in catalogue.items():
        headlines = results_by_group.get(key, [])
        kept = [h for h in headlines if within_window(h, max_age_hours)]
        kept = sort_newest_first(dedupe(kept))
        view[key] = {
            "label": meta["label"],
            "tier": meta["tier"],
            "origins": meta["origins"],
            "count": len(kept),
            "newest": kept[0].published_pt if kept else None,
            "headlines": [h.as_dict() for h in kept],
        }
    return view


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def plan_fetches(per_source: int) -> list[dict]:
    """Every network call this run will make, as plain job descriptions."""
    jobs: list[dict] = []
    for topic, (label, queries) in TOPIC_QUERIES.items():
        for query in queries:
            jobs.append({
                "url": google_news_url(query), "group": topic, "group_label": label,
                "tier": "topic", "origin": query, "limit": per_source, "fallback_source": None,
            })
    for key, (label, url, tier) in PUBLISHER_FEEDS.items():
        jobs.append({
            "url": url, "group": key, "group_label": label, "tier": tier,
            "origin": url, "limit": per_source, "fallback_source": label,
        })
    for key, (label, query) in PREMIUM_SITES.items():
        jobs.append({
            "url": google_news_url(query), "group": key, "group_label": label,
            "tier": "premium", "origin": query, "limit": per_source * 2, "fallback_source": label,
        })
    return jobs


def run_fetches(jobs: list[dict], workers: int, timeout: int, now: datetime) -> tuple[dict, list]:
    """Concurrent pull. Bounded pool -- these are public feeds being read once
    each, and a courteous concurrency beats hammering them."""
    by_group: dict[str, list[Headline]] = defaultdict(list)
    errors: list[dict] = []
    completed = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                fetch_one, job["url"], job["group"], job["group_label"], job["tier"],
                job["origin"], job["limit"], timeout, now, job["fallback_source"],
            ): job
            for job in jobs
        }
        for future in as_completed(futures):
            result = future.result()
            completed += 1
            if completed % 25 == 0:
                print(f"  ...{completed}/{len(jobs)} feeds fetched")
            if result.error:
                errors.append({"group": result.group, "origin": result.origin, "error": result.error})
            by_group[result.group].extend(result.headlines)
    return by_group, errors


def build_catalogue() -> dict[str, dict]:
    catalogue: dict[str, dict] = {}
    for topic, (label, queries) in TOPIC_QUERIES.items():
        catalogue[topic] = {"label": label, "tier": "topic", "origins": queries}
    for key, (label, url, tier) in PUBLISHER_FEEDS.items():
        catalogue[key] = {"label": label, "tier": tier, "origins": [url]}
    for key, (label, query) in PREMIUM_SITES.items():
        catalogue[key] = {"label": label, "tier": "premium", "origins": [query]}
    return catalogue


def split_by_tier(view: dict[str, dict]) -> dict[str, dict]:
    """Group the flat view into the renderer's four reading sections."""
    buckets: dict[str, dict] = {"topics": {}, "wires": {}, "primary": {}, "premium": {}}
    tier_to_bucket = {"topic": "topics", "wire": "wires", "primary": "primary", "premium": "premium"}
    for key, group in view.items():
        buckets[tier_to_bucket[group["tier"]]][key] = group
    return buckets


def write_payload(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("date", nargs="?", default=str(date.today()), help="YYYY-MM-DD label for the output files")
    parser.add_argument("--daily-hours", type=int, default=36, help="daily freshness window (default: 36)")
    parser.add_argument("--weekly-hours", type=int, default=168, help="weekly freshness window (default: 168)")
    parser.add_argument("--per-source", type=int, default=12, help="max headlines per query/feed (default: 12)")
    parser.add_argument("--workers", type=int, default=12, help="concurrent fetch workers (default: 12)")
    parser.add_argument("--timeout", type=int, default=12, help="per-feed timeout in seconds (default: 12)")
    args = parser.parse_args()

    if args.daily_hours > args.weekly_hours:
        parser.error("--daily-hours must not exceed --weekly-hours")

    now = datetime.now(timezone.utc)
    jobs = plan_fetches(args.per_source)
    print(
        f"Fetching news for {args.date}: {len(jobs)} feeds "
        f"({len(TOPIC_QUERIES)} topics x 3 queries, {len(PUBLISHER_FEEDS)} publisher feeds, "
        f"{len(PREMIUM_SITES)} premium desks), {args.workers} workers..."
    )

    by_group, errors = run_fetches(jobs, args.workers, args.timeout, now)
    catalogue = build_catalogue()

    raw_total = sum(len(v) for v in by_group.values())
    print(f"  {raw_total} raw items pulled; {len(errors)} feed error(s)")

    outputs = {}
    for mode, window in (("daily", args.daily_hours), ("weekly", args.weekly_hours)):
        view = assemble_view(by_group, catalogue, window)
        buckets = split_by_tier(view)
        rollup = build_rollup(view, now, window)
        payload = {
            "generated_at_utc": now.isoformat(timespec="seconds"),
            "generated_at_pt": now.astimezone(PACIFIC).strftime("%A, %B %-d %Y at %-I:%M %p PT"),
            "asof_date": args.date,
            "mode": mode,
            "window_hours": window,
            "per_source_limit": args.per_source,
            "source_counts": {
                "topic_queries": sum(len(q) for _, q in TOPIC_QUERIES.values()),
                "publisher_feeds": len(PUBLISHER_FEEDS),
                "premium_desks": len(PREMIUM_SITES),
            },
            "totals": {
                "headlines": rollup["total_headlines"],
                "distinct_sources": rollup["distinct_sources"],
                "topics": len(buckets["topics"]),
            },
            "topics": buckets["topics"],
            "wires": buckets["wires"],
            "primary": buckets["primary"],
            "premium": buckets["premium"],
            "rollup": rollup,
            "fetch_errors": errors,
        }
        suffix = "-news.json" if mode == "daily" else "-news-weekly.json"
        path = DATA_DIR / f"{args.date}{suffix}"
        write_payload(path, payload)
        outputs[mode] = (path, payload)
        print(f"Wrote {path}  ({rollup['total_headlines']} headlines, {rollup['distinct_sources']} sources, {window}h window)")

    daily_payload = outputs["daily"][1]
    print("\n--- Daily headline counts by topic ---")
    for key, group in sorted(daily_payload["topics"].items(), key=lambda kv: kv[1]["count"], reverse=True):
        print(f"  {group['label']:28s} {group['count']:3d}")

    print("\n--- Premium desks (daily) ---")
    for key, group in daily_payload["premium"].items():
        print(f"  {group['label']:28s} {group['count']:3d}")

    if errors:
        print("\n--- Feed errors ---")
        for err in errors:
            print(f"  {err['group']}: {err['error'][:90]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
