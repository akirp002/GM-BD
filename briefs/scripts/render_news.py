#!/usr/bin/env python3
"""
Render the daily + weekly news pulls into one interactive HTML reader.

Both windows ship in a single page: every headline carries an `is_daily`
flag, so the Daily/Weekly switch is a CSS class flip rather than a second
payload or a re-render. The page is built from the weekly JSON (the wider
set) with the daily window read off the daily JSON so the two never drift.

Usage:
    python3 render_news.py [YYYY-MM-DD]

Input:
    briefs/data/<date>-news-weekly.json   (required -- the content)
    briefs/data/<date>-news.json          (optional -- supplies the daily window)

Output:
    briefs/<date>-news.html

Headlines are third-party text, so this renderer escapes everything
(autoescape=True) -- unlike render_dashboard.py, whose narrative JSON carries
deliberate inline markup authored by hand.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_news import normalize_title  # noqa: E402  (same-dir sibling import)
from lib_news_digest import (  # noqa: E402
    build_attention_shift,
    build_corroboration,
    build_playbook_volume,
    load_json,
)

ROOT = Path(__file__).resolve().parents[1]  # briefs/
DATA_DIR = ROOT / "data"
TEMPLATE_DIR = ROOT / "templates"
TEMPLATE_NAME = "news.html.j2"

FRONTPAGE_SIZE = 24
MAX_FRONTPAGE_PER_SOURCE = 2
FRESH_HOURS = 6.0
TOP_SOURCES = 18
TOP_TOPICS = 14

# Only http(s) links are emitted as anchors. RSS <guid> occasionally carries a
# non-URL identifier, and a javascript:/data: value would be an injection
# vector even with autoescape on, since it lands in an href attribute.
SAFE_SCHEMES = ("http://", "https://")


def load_payload(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"Missing {path}.\nRun: python3 briefs/scripts/fetch_news.py {path.stem.split('-news')[0]}"
        )
    return json.loads(path.read_text())


def safe_link(link: Optional[str]) -> Optional[str]:
    if not link:
        return None
    return link if link.startswith(SAFE_SCHEMES) else None


def prepare_headline(raw: dict, daily_window: float) -> dict:
    """Shape one stored headline for the template: daily flag, freshness, and
    the lowercased blob the client-side filter matches against."""
    age = raw.get("age_hours")
    title = raw.get("title") or ""
    source = raw.get("source") or ""
    summary = raw.get("summary") or ""
    return {
        "title": title,
        "link": safe_link(raw.get("link")),
        "source": source or None,
        "summary": summary or None,
        "published_utc": raw.get("published_utc"),
        "published_pt": raw.get("published_pt"),
        "age_label": raw.get("age_label") or "undated",
        "age_hours": age,
        # Undated items stay in the daily view for the same reason the fetcher
        # keeps them: an unknown timestamp is not evidence of staleness.
        "is_daily": age is None or age <= daily_window,
        "is_fresh": age is not None and age <= FRESH_HOURS,
        "filter_text": f"{title} {source} {summary}".lower(),
    }


def prepare_groups(bucket: dict[str, dict], daily_window: float) -> list[dict]:
    """Preserve the fetcher's insertion order -- it encodes reading priority,
    which a count-sort would throw away."""
    groups = []
    for key, group in bucket.items():
        headlines = [prepare_headline(h, daily_window) for h in group["headlines"]]
        groups.append({
            "id": key,
            "label": group["label"],
            "origins": group.get("origins", []),
            "count": len(headlines),
            "newest": group.get("newest"),
            "headlines": headlines,
        })
    return groups


def build_frontpage(
    all_groups: list[dict],
    limit: int = FRONTPAGE_SIZE,
    max_per_source: int = MAX_FRONTPAGE_PER_SOURCE,
) -> list[dict]:
    """Newest stories across every tier, deduped -- the 'what happened since I
    last looked' view you get before deciding which section to read.

    Capped per outlet: the highest-frequency wires publish an order of
    magnitude more often than the desks worth reading first, and without a cap
    they crowd everything else off the page purely on recency."""
    seen: set[str] = set()
    pooled: list[dict] = []
    for group in all_groups:
        for headline in group["headlines"]:
            key = normalize_title(headline["title"])
            if not key or key in seen:
                continue
            seen.add(key)
            pooled.append(headline)
    pooled.sort(key=lambda h: (h["age_hours"] is None, h["age_hours"] if h["age_hours"] is not None else 0.0))

    per_source: dict[str, int] = {}
    frontpage: list[dict] = []
    for headline in pooled:
        source = headline.get("source") or "unattributed"
        if per_source.get(source, 0) >= max_per_source:
            continue
        per_source[source] = per_source.get(source, 0) + 1
        frontpage.append(headline)
        if len(frontpage) >= limit:
            break
    return frontpage


def build_week_in_review(rollup: dict, daily_window: float) -> list[dict]:
    days = []
    for day in rollup.get("by_day", []):
        days.append({
            "date": day["date"],
            "label": day["label"],
            "count": day["count"],
            "top_sources": day.get("top_sources", [])[:4],
            "headlines": [prepare_headline(h, daily_window) for h in day["headlines"]],
        })
    return days


def with_pct(rows: list[dict], value_key: str = "count") -> list[dict]:
    """Bar widths as a share of the largest row, so a panel reads as a
    ranking without needing an axis."""
    top = max((row[value_key] for row in rows), default=0) or 1
    return [dict(row, pct=round(row[value_key] / top * 100, 1)) for row in rows]


def render(date_str: str) -> Path:
    weekly = load_payload(DATA_DIR / f"{date_str}-news-weekly.json")
    daily_path = DATA_DIR / f"{date_str}-news.json"
    daily_window = (
        json.loads(daily_path.read_text()).get("window_hours", 36) if daily_path.exists() else 36
    )
    weekly_window = weekly.get("window_hours", 168)

    topics = prepare_groups(weekly.get("topics", {}), daily_window)
    wires = prepare_groups(weekly.get("wires", {}), daily_window)
    primary = prepare_groups(weekly.get("primary", {}), daily_window)
    premium = prepare_groups(weekly.get("premium", {}), daily_window)

    frontpage = build_frontpage(premium + topics + wires + primary)
    rollup = weekly.get("rollup", {})
    week_in_review = build_week_in_review(rollup, daily_window)

    daily_total = sum(1 for g in topics + wires + primary + premium for h in g["headlines"] if h["is_daily"])
    weekly_total = sum(len(g["headlines"]) for g in topics + wires + primary + premium)

    meta = {
        "title": "Global Macro Newsroom",
        "description": (
            f"Daily and weekly cross-asset news reader for {date_str} -- "
            f"{weekly_total} stories from {rollup.get('distinct_sources', 0)} outlets, "
            "topic desks, newsroom wires, primary sources and premium desks."
        ),
        "asof_date": date_str,
        "generated_at_pt": weekly.get("generated_at_pt", ""),
        "daily_window": int(daily_window),
        "weekly_window": int(weekly_window),
        "daily_total": daily_total,
        "weekly_total": weekly_total,
        "frontpage_intro": (
            f"The newest {len(frontpage)} stories across every source this tool reads, deduplicated and "
            f"ordered by timestamp. Switch to Weekly above to widen the window from "
            f"{int(daily_window)} hours to {int(weekly_window) // 24} days; every timestamp is Pacific, "
            "and hovering a relative age shows the exact publication time."
        ),
        "premium_intro": (
            "Headlines from the paywalled desks, surfaced so you can see what they are leading with without "
            "opening them. The bodies stay behind their paywalls, so these are reading signals: confirm any "
            "number against a free source before it reaches the brief."
        ),
        "wires_intro": (
            "Newsroom feeds read directly rather than through search, so these arrive in the order the desk "
            "published them."
        ),
        "primary_intro": (
            "The institutions themselves -- central banks, Treasury, and the statistical agencies. When a "
            "number here disagrees with a news write-up of it, this is the one that counts."
        ),
        "week_intro": (
            "The weekly window bucketed by publication day, newest first, so a week reads as a sequence of "
            "days rather than one long list. Available in Weekly mode."
        ),
        "ledger_intro": (
            "What this run actually reached: which outlets dominated the week, which topics carried the most "
            "volume, and any feed that failed so a thin section is never mistaken for a quiet market."
        ),
    }

    # The same computed readings the dashboard carries, so the two pages agree
    # by construction rather than by both being edited in step.
    daily_payload = load_json(daily_path) or {}
    analysis = None
    if daily_payload:
        attention = build_attention_shift(daily_payload, weekly)
        playbook = [row for row in build_playbook_volume(daily_payload, weekly) if row["today"]]
        analysis = {
            "attention": with_pct(attention, value_key="ratio"),
            "corroboration": build_corroboration(daily_payload),
            "playbook": with_pct(playbook, value_key="today"),
        }

    # The day's researched top stories, if the brief pipeline has produced
    # them -- the analysis layer a headline list cannot supply on its own.
    narrative = load_json(DATA_DIR / f"{date_str}-narrative.json") or {}
    top_stories = narrative.get("top_stories", [])

    ledger = {
        "top_sources": with_pct(rollup.get("top_sources", [])[:TOP_SOURCES]),
        "topic_volume": with_pct([r for r in rollup.get("topic_volume", []) if r["count"]][:TOP_TOPICS]),
        "counts": weekly.get("source_counts", {}),
        "distinct_sources": rollup.get("distinct_sources", 0),
        "fetch_errors": weekly.get("fetch_errors", []),
    }

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(enabled_extensions=("j2", "html"), default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    html = env.get_template(TEMPLATE_NAME).render(
        meta=meta,
        frontpage=frontpage,
        topics=topics,
        wires=wires,
        primary=primary,
        premium=premium,
        week_in_review=week_in_review,
        ledger=ledger,
        analysis=analysis,
        top_stories=top_stories,
    )

    out_path = ROOT / f"{date_str}-news.html"
    out_path.write_text(html)
    return out_path


def main() -> int:
    date_str = sys.argv[1] if len(sys.argv) > 1 else str(date.today())
    out_path = render(date_str)
    size_kb = out_path.stat().st_size / 1024
    print(f"Wrote {out_path} ({size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
