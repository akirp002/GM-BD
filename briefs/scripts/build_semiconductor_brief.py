#!/usr/bin/env python3
"""Build a semiconductor industry and market-structure research brief.

Usage:
    python3 briefs/scripts/build_semiconductor_brief.py [YYYY-MM-DD]
    python3 briefs/scripts/build_semiconductor_brief.py YYYY-MM-DD --render-only

The script is intentionally independent of the morning brief's fetchers and
renderers. It writes a dedicated data file and HTML page, while following the
same dated-data, authored-research, standalone-template workflow.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import hashlib
import io

import pandas as pd

from jinja2 import Environment, FileSystemLoader, select_autoescape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib_semi_analytics as sa  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
TEMPLATE_DIR = ROOT / "templates"
KNOWLEDGE_PATH = ROOT / "semiconductors" / "knowledge.json"
ARCHIVE_PATH = ROOT / "semiconductors" / "headline_archive.jsonl"
DEFAULT_RATES_PATH = ROOT / "semiconductors" / "default_rates.json"
TEMPLATE_NAME = "semiconductor.html.j2"
USER_AGENT = "Mozilla/5.0 (compatible; SemiconductorBrief/1.0; +local-research-tool)"
NEWS_WINDOW = timedelta(days=7)
MAX_NEWS_AGE = timedelta(days=95)  # deepest look-back pulled from the feed

NEWS_DESKS: dict[str, tuple[str, tuple[str, ...]]] = {
    "cycle": ("Industry cycle & end-market demand", (
        "semiconductor chip sales demand forecast inventory cycle",
        "WSTS semiconductor market forecast chip demand",
    )),
    "compute_memory": ("AI compute, HBM & memory", (
        "AI accelerator semiconductor HBM memory supply demand",
        "DRAM NAND HBM pricing inventory semiconductor",
    )),
    "foundry": ("Foundries, nodes & capacity", (
        "TSMC Samsung Intel foundry advanced process node capacity",
        "semiconductor foundry utilization wafer pricing capex",
    )),
    "equipment": ("Equipment, materials & lithography", (
        "ASML EUV lithography semiconductor manufacturing equipment",
        "semiconductor wafer fab equipment bookings materials",
    )),
    "packaging": ("Advanced packaging & OSAT", (
        "advanced packaging CoWoS chiplet OSAT capacity substrate",
        "semiconductor packaging bottleneck silicon interposer",
    )),
    "policy": ("Trade, policy & supply-chain resilience", (
        "semiconductor export controls China chip restrictions",
        "CHIPS Act semiconductor subsidies fab supply chain",
    )),
    "company": ("Company results, margins & guidance", (
        "semiconductor earnings guidance gross margin inventory",
        "chipmaker earnings capital expenditure guidance",
    )),
    "market_structure": ("Semiconductor equity market structure", (
        "semiconductor stocks ETF flows SOXX SMH positioning",
        "chip stocks options dealer gamma short interest liquidity",
    )),
}

# Derived from lib_semi_analytics.SEGMENTS so the tape, segments and dashboard
# share one definition. Returns and volume are market observations, not
# estimates of fundamentals, order-book liquidity, or investor flows.
UNIVERSE: dict[str, dict[str, str]] = {
    **{t: {"name": sa.NAMES.get(t, t), "group": "ETF"} for t in sa.ETFS},
    **{t: {"name": sa.NAMES.get(t, t), "group": "Benchmark"} for t in sa.BENCHMARKS},
    **{t: {"name": sa.NAMES.get(t, t), "group": seg["label"]}
       for seg in sa.SEGMENTS.values() for t in seg["tickers"]},
}

PERIODS: tuple[tuple[str, int | None], ...] = (
    ("1D", 1), ("1W", 5), ("1M", 21), ("3M", 63), ("YTD", None),
)


def normalize_title(title: str) -> str:
    """Normalize headline text for cross-query deduplication."""
    return re.sub(r"\W+", " ", title.casefold()).strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _child_text(element: ET.Element, *names: str) -> str | None:
    wanted = {name.casefold() for name in names}
    for child in element:
        if _local_name(child.tag) in wanted:
            text = "".join(child.itertext()).strip()
            return text or None
    return None


def _parse_published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_news_feed(
    payload: bytes,
    category: str,
    now: datetime | None = None,
    max_age: timedelta = NEWS_WINDOW,
) -> list[dict[str, Any]]:
    """Parse RSS/Atom entries, retaining undated stories with explicit null dates."""
    root = ET.fromstring(payload)
    now = now or datetime.now(timezone.utc)
    cutoff = now.astimezone(timezone.utc) - max_age
    stories: list[dict[str, Any]] = []
    for element in root.iter():
        if _local_name(element.tag) not in {"item", "entry"}:
            continue
        title = _child_text(element, "title")
        if not title:
            continue
        published = _parse_published(_child_text(element, "pubDate", "published", "updated"))
        if published and published < cutoff:
            continue
        link = _child_text(element, "link")
        if not link:
            for child in element:
                if _local_name(child.tag) == "link":
                    link = child.attrib.get("href")
                    if link:
                        break
        source = _child_text(element, "source", "author") or "Source not supplied"
        summary = _child_text(element, "description", "summary", "content")
        stories.append({
            "title": title,
            "link": link,
            "source": source,
            "published": published.isoformat(timespec="seconds") if published else None,
            "summary": summary,
            "category": category,
        })
    return stories


def dedupe_headlines(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the freshest copy of a headline returned by multiple queries."""
    unique: dict[str, dict[str, Any]] = {}
    for story in stories:
        key = normalize_title(story["title"])
        if not key:
            continue
        prior = unique.get(key)
        if prior is None or (story.get("published") or "") > (prior.get("published") or ""):
            unique[key] = story
    return sorted(
        unique.values(),
        key=lambda item: item.get("published") or "",
        reverse=True,
    )


def story_id(title: str) -> str:
    """Stable short id so a synthesis can cite a headline and be audited."""
    return "H" + hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()[:7]


def assign_window(story: dict[str, Any], now: datetime) -> None:
    published = _parse_published(story.get("published"))
    age = (now - published).total_seconds() / 86400 if published else 0.0
    story["window"] = sa.window_for(age)
    story["id"] = story_id(story["title"])


def fetch_semiconductor_news(now: datetime | None = None) -> tuple[dict[str, list], list[str]]:
    """Pull focused Google News RSS searches over three look-back windows.

    week = every desk query; month (8-30d) and quarter (31-90d) use each desk's
    lead query with after:/before: so trend questions can be answered from more
    than the last seven days. Individual feed failures are reported, not fatal.
    """
    now = now or datetime.now(timezone.utc)
    by_category: dict[str, list] = {category: [] for category in NEWS_DESKS}
    errors: list[str] = []
    day = lambda n: (now - timedelta(days=n)).date().isoformat()  # noqa: E731
    jobs = []
    for category, (label, category_queries) in NEWS_DESKS.items():
        for query in category_queries:
            jobs.append((category, label, query, f"{query} when:7d"))
        lead = category_queries[0]
        jobs.append((category, label, lead, f"{lead} after:{day(30)} before:{day(7)}"))
        jobs.append((category, label, lead, f"{lead} after:{day(90)} before:{day(30)}"))

    def fetch_query(category: str, label: str, query: str, q: str):
        params = urllib.parse.urlencode({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        request = urllib.request.Request(
            f"https://news.google.com/rss/search?{params}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return category, parse_news_feed(response.read(), category, now, MAX_NEWS_AGE), None
        except (OSError, urllib.error.URLError, ET.ParseError) as error:
            return category, [], f"{label} ({q}): {error}"

    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(fetch_query, *job) for job in jobs]
        for future in as_completed(futures):
            category, stories, error = future.result()
            by_category[category].extend(stories)
            if error:
                errors.append(error)
    by_category = {c: dedupe_headlines(st) for c, st in by_category.items()}
    for stories in by_category.values():
        for story in stories:
            assign_window(story, now)
    return by_category, errors


def update_archive(stories: list[dict[str, Any]], as_of: str) -> list[dict[str, Any]]:
    """Append unseen headlines to a durable archive and return prior entries.

    The RSS feed only reaches ~90 days; the archive is what lets later runs
    reason about months-long trends. Returned entries are those first seen
    before today, newest first.
    """
    seen: dict[str, dict[str, Any]] = {}
    if ARCHIVE_PATH.exists():
        for line in ARCHIVE_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    row = json.loads(line)
                    seen[row["id"]] = row
                except (json.JSONDecodeError, KeyError):
                    continue
    fresh = []
    for story in stories:
        if story["id"] not in seen:
            row = {"id": story["id"], "title": story["title"], "source": story["source"],
                   "published": story.get("published"), "category": story["category"],
                   "link": story.get("link"), "first_seen": as_of}
            seen[row["id"]] = row
            fresh.append(row)
    if fresh:
        ARCHIVE_PATH.parent.mkdir(exist_ok=True)
        with ARCHIVE_PATH.open("a", encoding="utf-8") as handle:
            for row in fresh:
                handle.write(json.dumps(row, ensure_ascii=False) + chr(10))
    return sorted(seen.values(), key=lambda r: r.get("published") or "", reverse=True)


def _return_pct(series: Any, periods: int | None) -> float | None:
    clean = series.dropna()
    if clean.empty:
        return None
    if periods is None:
        same_year = clean[clean.index.year == clean.index[-1].year]
        if same_year.empty:
            return None
        base = float(same_year.iloc[0])
    else:
        if len(clean) <= periods:
            return None
        base = float(clean.iloc[-periods - 1])
    last = float(clean.iloc[-1])
    if base == 0:
        return None
    return round((last / base - 1) * 100, 2)


def build_market_snapshot(history: Any) -> dict[str, Any]:
    """Summarize Yahoo Finance price/volume history without imputing missing data."""
    close = history["Close"]
    volume = history["Volume"]
    rows: dict[str, dict[str, Any]] = {}
    for ticker, info in UNIVERSE.items():
        if ticker not in close.columns:
            continue
        prices = close[ticker].dropna()
        if prices.empty:
            continue
        latest = float(prices.iloc[-1])
        ticker_volume = volume[ticker].dropna() if ticker in volume.columns else []
        avg_dollar_volume = None
        if len(ticker_volume):
            recent_volumes = ticker_volume.tail(20)
            avg_dollar_volume = round(float(recent_volumes.mean()) * latest, 0)
        rows[ticker] = {
            "name": info["name"],
            "group": info["group"],
            "price": round(latest, 2),
            "as_of": str(prices.index[-1].date()),
            "returns_pct": {
                period: _return_pct(prices, sessions)
                for period, sessions in PERIODS
            },
            "avg_dollar_volume_20d": avg_dollar_volume,
        }

    company_returns = [
        row["returns_pct"]["1W"]
        for ticker, row in rows.items()
        if row["group"] not in {"ETF", "Benchmark"} and row["returns_pct"]["1W"] is not None
    ]
    weekly_values = [
        row["returns_pct"]["1W"] for row in rows.values()
        if row["returns_pct"]["1W"] is not None
    ]
    company_median = statistics.median(company_returns) if company_returns else None
    universe_median = statistics.median(weekly_values) if weekly_values else None
    return {
        "as_of": max((row["as_of"] for row in rows.values()), default=None),
        "tickers": rows,
        "breadth_1w": {
            "positive": sum(value > 0 for value in company_returns),
            "observed": len(company_returns),
            "median_return_pct": round(company_median, 2) if company_median is not None else None,
        },
        "universe_median_1w_pct": round(universe_median, 2) if universe_median is not None else None,
        "measurement_note": (
            "Yahoo Finance adjusted prices and reported share volume. "
            "20-day average dollar volume is price × share-volume, a rough "
            "trading-activity proxy, not order-book depth, ETF flow, or ownership."
        ),
    }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(f"Required input not found: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {path}: {error}") from error


def _research_path(as_of: str) -> Path:
    return DATA_DIR / f"{as_of}-semiconductor-research.json"


def ensure_research_draft(as_of: str) -> Path:
    path = _research_path(as_of)
    if not path.exists():
        path.write_text(json.dumps({
            "as_of": as_of,
            "top_stories": [],
            "watchlist": [],
            "learning_notes": [],
        }, indent=2) + "\n", encoding="utf-8")
    return path


def _safe_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urllib.parse.urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def build_context(as_of: str, data: dict[str, Any], research: dict[str, Any],
                  knowledge: dict[str, Any]) -> dict[str, Any]:
    desks = []
    lookup: dict[str, dict[str, Any]] = {}
    for story in data.get("news", {}).get("all", []):
        story["safe_link"] = _safe_url(story.get("link"))
        story.setdefault("id", story_id(story["title"]))
        story.setdefault("window", "week")
        lookup[story["id"]] = story
    for row in data.get("news", {}).get("archive", []):
        row["safe_link"] = _safe_url(row.get("link"))
        lookup.setdefault(row["id"], row)
    for category, (label, _queries) in NEWS_DESKS.items():
        items = data.get("news", {}).get(category, [])
        for item in items:
            item["safe_link"] = _safe_url(item.get("link"))
        desks.append({"id": category, "label": label, "headlines": items,
                      "week": sum(1 for i in items if i.get("window") == "week")})

    def resolve(ids: list[str] | None) -> list[dict[str, Any]]:
        return [lookup[i] for i in (ids or []) if i in lookup]

    for collection in ("top_stories", "watchlist", "learning_notes"):
        for item in research.get(collection, []):
            for source in item.get("sources", []):
                source["safe_url"] = _safe_url(source.get("url"))
            item["safe_url"] = _safe_url(item.get("url"))
    for horizon in research.get("horizons", {}).values():
        horizon["cited"] = resolve(horizon.get("source_ids"))
    for theme in research.get("themes", []):
        theme["cited"] = resolve(theme.get("source_ids"))
        for ev in theme.get("evidence", []):
            ev["cited"] = resolve(ev.get("source_ids"))
    for link in research.get("industry_linkages", []):
        link["cited"] = resolve(link.get("source_ids"))

    market = data.get("market", {})
    analytics = market.get("analytics", {})
    dashboard = {
        "analytics": analytics, "macro": data.get("macro", {}),
        "segment_order": list(analytics.get("segment_defs", {})),
    }
    return {
        "as_of": as_of,
        "generated_at": data.get("generated_at"),
        "market": market,
        "analytics": analytics,
        "macro": data.get("macro", {}),
        "dashboard": dashboard,
        "desks": desks,
        "headline_count": len(data.get("news", {}).get("all", [])),
        "window_counts": data.get("news", {}).get("window_counts", {}),
        "fetch_errors": data.get("fetch_errors", []),
        "data_source_notes": data.get("source_notes", []),
        "research": research,
        "knowledge": knowledge,
    }


def render_html(context: dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(("html", "j2")),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(TEMPLATE_NAME)
    return template.render(**context)


CHART_SESSIONS = 252


def build_analytics(history: Any) -> dict[str, Any]:
    """Sub-industry indexes, correlations, dispersion and per-ticker closes.

    Closes are forward-filled across non-overlapping exchange holidays (Korea,
    Taiwan) and restricted to US sessions, so daily returns line up. A filled
    holiday contributes a zero return; it is not an observed price.
    """
    close = history["Close"].copy()
    anchor = "SPY" if "SPY" in close.columns else close.columns[0]
    close = close[close[anchor].notna()].ffill()
    seg_idx, members = sa.segment_indexes(close)
    seg_ret = seg_idx.pct_change(fill_method=None)
    bench = close["SOXX"] if "SOXX" in close.columns else None

    segments: dict[str, Any] = {}
    rel = sa.beta_and_relative(seg_idx, bench) if bench is not None else {}
    for sid in seg_idx.columns:
        segments[sid] = {
            "label": sa.SEGMENTS[sid]["label"], "members": members[sid],
            "returns_pct": sa.returns_table(seg_idx[sid]),
            "drawdown": sa.drawdown_from_high(seg_idx[sid]),
            **rel.get(sid, {}),
        }
    corr = {}
    for window in (20, 60, 120):
        matrix = sa.correlation_matrix(seg_ret, window)
        corr[str(window)] = {"matrix": sa.matrix_to_dict(matrix), "pairs": sa.top_pairs(matrix)}
    tail = close.tail(CHART_SESSIONS)
    stock_cols = [t for t in sa.all_segment_tickers() if t in close.columns]
    return {
        "dates": [str(d.date()) for d in tail.index],
        "series": {t: [None if v != v else round(float(v), 2) for v in tail[t]]
                   for t in close.columns},
        "names": {t: sa.NAMES.get(t, t) for t in close.columns},
        "segments": segments,
        "segment_defs": {sid: {"label": seg["label"], "tickers": [t for t in seg["tickers"] if t in close.columns]}
                         for sid, seg in sa.SEGMENTS.items()},
        "etfs": [t for t in sa.ETFS if t in close.columns],
        "benchmarks": [t for t in sa.BENCHMARKS if t in close.columns],
        "correlation": corr,
        "correlation_shifts_60d": sa.correlation_shifts(seg_ret, 60),
        "dispersion_1m": sa.dispersion(close, stock_cols, 21),
        "dispersion_3m": sa.dispersion(close, stock_cols, 63),
        "soxx_drawdown": sa.drawdown_from_high(bench) if bench is not None else {},
        "_close": close,  # stripped before serialisation
    }


def _read_fred(series_id: str, attempts: int = 3) -> pd.Series | None:
    """FRED's CSV endpoint is slow and rejects some custom User-Agents, so use
    pandas' default client (as fetch_market_data.py does) and retry."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    for _ in range(attempts):
        try:
            frame = pd.read_csv(url, parse_dates=["observation_date"], storage_options={"timeout": 30})
        except Exception:  # noqa: BLE001 - failure is recorded by the caller
            continue
        frame = frame.rename(columns={frame.columns[1]: "value"})
        frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
        return frame.dropna().set_index("observation_date")["value"]
    return None


def fetch_macro(analytics: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Rates, credit spreads, bank credit-quality rates and semis-vs-credit links."""
    errors: list[str] = []
    wanted = {**sa.MACRO_SERIES, **sa.CREDIT_QUALITY_SERIES}
    pulled: dict[str, pd.Series | None] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_read_fred, sid): sid for sid in wanted}
        for future in as_completed(futures):
            pulled[futures[future]] = future.result()
    series_out: dict[str, Any] = {}
    for sid, meta in wanted.items():
        s = pulled.get(sid)
        if s is None or s.empty:
            errors.append(f"FRED {sid} ({meta['label']}): no data returned")
            continue
        series_out[sid] = {
            **meta, "id": sid, **sa.macro_stats(s, meta["unit"]),
            "history": sa.downsample(s, 600 if meta["unit"] != "pct_level" else 80),
        }
    hy = pulled.get("BAMLH0A0HYM2")
    implied = {}
    if hy is not None and not hy.empty:
        implied = {
            "hy_oas_pct": round(float(hy.iloc[-1]), 2),
            "spread_implied_annual_loss_pct": sa.spread_implied_loss(float(hy.iloc[-1])),
            "assumed_recovery": sa.ASSUMED_RECOVERY,
            "note": ("OAS / (1 - recovery). Mixes default compensation with liquidity and risk "
                     "premia: a stress gauge, not a measured default rate."),
        }
    links: dict[str, Any] = {}
    if analytics is not None and "SOXX" in analytics["_close"].columns:
        soxx_ret = analytics["_close"]["SOXX"].pct_change(fill_method=None) * 100
        for sid, label in (("BAMLH0A0HYM2", "HY OAS (pp change)"), ("DGS10", "10Y yield (pp change)"),
                           ("DFII10", "10Y real yield (pp change)"), ("VIXCLS", "VIX (pt change)")):
            s = pulled.get(sid)
            if s is None:
                continue
            aligned = s.reindex(soxx_ret.index).ffill()
            for window in (60, 120):
                res = sa.rolling_corr_with(aligned, soxx_ret, window)
                links.setdefault(sid, {"label": label, "vs": "SOXX daily % return"})[f"corr_{window}d"] = res["corr"]
    manual = None
    if DEFAULT_RATES_PATH.exists():
        try:
            manual = json.loads(DEFAULT_RATES_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            errors.append(f"default_rates.json is invalid JSON: {error}")
    return {
        "series": series_out, "hy_implied_loss": implied, "semis_credit_links": links,
        "manual_default_rates": manual,
        "notes": [
            "Treasury yields, ICE BofA OAS and VIX come from FRED (daily; OAS history is limited by FRED's licensing window).",
            "Bank delinquency / charge-off rates are quarterly Federal Reserve series: loan-book credit quality, not bond default rates.",
            "Rated-bond default rates (Moody's / S&P / Fitch) are licensed; add them to briefs/semiconductors/default_rates.json to show them.",
        ],
    }, errors


def fetch_market_data() -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Download the semiconductor tape once, recording incomplete coverage."""
    import yfinance as yf

    tickers = sa.download_universe()
    errors: list[str] = []
    analytics = None
    try:
        history = yf.download(tickers, period="2y", progress=False, auto_adjust=True, threads=True)
        if history.empty:
            raise RuntimeError("Yahoo Finance returned no price history")
        snapshot = build_market_snapshot(history)
        analytics = build_analytics(history)
    except Exception as error:  # noqa: BLE001
        errors.append(f"Yahoo Finance semiconductor market data: {error}")
        snapshot = {
            "as_of": None, "tickers": {},
            "breadth_1w": {"positive": 0, "observed": 0, "median_return_pct": None},
            "universe_median_1w_pct": None,
            "measurement_note": "Market data unavailable; see the fetch-error ledger.",
        }
    missing = sorted(set(tickers) - set(snapshot["tickers"]))
    if missing:
        errors.append(f"Yahoo Finance returned no usable rows for: {', '.join(missing)}")
    macro, macro_errors = fetch_macro(analytics)
    if analytics:
        analytics = {k: v for k, v in analytics.items() if not k.startswith("_")}
    return {**snapshot, "analytics": analytics or {}}, macro, errors + macro_errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", nargs="?", default=str(date.today()), help="Output date (YYYY-MM-DD)")
    parser.add_argument("--render-only", action="store_true", help="Render from existing semiconductor data")
    args = parser.parse_args()
    try:
        as_of = date.fromisoformat(args.date).isoformat()
    except ValueError:
        parser.error(f"invalid date {args.date!r}; expected YYYY-MM-DD")

    DATA_DIR.mkdir(exist_ok=True)
    data_path = DATA_DIR / f"{as_of}-semiconductor-data.json"
    output_path = ROOT / f"{as_of}-semiconductor.html"

    if args.render_only:
        data = _load_json(data_path)
    else:
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        news, news_errors = fetch_semiconductor_news()
        market, macro, market_errors = fetch_market_data()
        all_stories = dedupe_headlines([
            story for items in news.values() for story in items
        ])
        archive_rows = update_archive(all_stories, as_of)
        live_ids = {story["id"] for story in all_stories}
        window_counts: dict[str, int] = {}
        for story in all_stories:
            window_counts[story["window"]] = window_counts.get(story["window"], 0) + 1
        older = [row for row in archive_rows if row["id"] not in live_ids][:150]
        data = {
            "as_of": as_of,
            "generated_at": generated_at,
            "news": {**news, "all": all_stories, "archive": older, "window_counts": window_counts},
            "market": market,
            "macro": macro,
            "fetch_errors": news_errors + market_errors,
            "source_notes": [
                "News headlines and summaries are RSS/search-feed records, not independently verified article facts.",
                "Market prices and volume are Yahoo Finance observations; check each row's as-of date.",
            ],
        }
        data_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {data_path}")

    research_path = ensure_research_draft(as_of)
    research = _load_json(research_path)
    knowledge = _load_json(KNOWLEDGE_PATH)
    context = build_context(as_of, data, research, knowledge)
    output_path.write_text(render_html(context), encoding="utf-8")
    print(f"Wrote {output_path}")
    print(f"Research draft: {research_path}")
    print(f"Headlines: {context['headline_count']} ({context['window_counts']}); "
          f"tickers: {len(data.get('market', {}).get('tickers', {}))}; "
          f"macro series: {len(data.get('macro', {}).get('series', {}))}")
    if not any(research.get(k) for k in ("themes", "horizons", "top_stories")):
        print("Next: python3 briefs/scripts/synthesize_semiconductor.py", as_of, file=sys.stderr)
    for error in data.get("fetch_errors", []):
        print(f"Fetch warning: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
