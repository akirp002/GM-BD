#!/usr/bin/env python3
"""
Turn the raw news pulls into an analysis layer both renderers can consume.

`fetch_news.py` produces headlines; this produces *readings* of them. Three
computed signals, all reproducible from files already on disk -- no authoring
step, no model call:

  1. Attention shift   -- today's per-topic volume against its own trailing
                          weekly daily-average, so "the newsroom moved onto
                          this today" is measured rather than asserted.
  2. Corroboration     -- how many distinct outlets carry a story cluster,
                          which is the brief's "count independent sources,
                          not articles" rule applied mechanically.
  3. Playbook exposure -- topic volume mapped onto the six playbook themes,
                          so the structural frame gets a live volume read.

Everything carries timestamps: each headline keeps its publication time, and
each derived figure states the window it was computed over.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# Topic keys emitted by fetch_news.py -> the playbook theme each one informs.
# A topic may feed more than one theme; a theme with no topics simply reports
# no volume rather than being dropped, so the frame stays stable day to day.
PLAYBOOK_THEMES: dict[str, dict[str, Any]] = {
    "fiscal_dominance": {
        "label": "Fiscal dominance",
        "topics": ["treasury_yields", "treasury_auctions", "deficit_issuance"],
    },
    "structural_inflation": {
        "label": "Structural inflation",
        "topics": ["econ_data", "fed_speakers", "central_banks", "labor_market", "energy_power"],
    },
    "geopolitical_fragmentation": {
        "label": "Geopolitical fragmentation",
        "topics": ["oil_geopolitics", "trade_tariffs", "china_macro", "em_macro"],
    },
    "private_credit": {
        "label": "Private-credit liquidity",
        "topics": ["private_credit", "credit", "ai_capex_financing"],
    },
    "leverage_basis": {
        "label": "Leverage / basis trade",
        "topics": ["vix_funding"],
    },
    "passive_concentration": {
        "label": "Passive concentration",
        "topics": ["market_structure"],
    },
}

# Signal words that mark a headline as argument rather than report. Used only
# to surface candidate analysis pieces for a human to read -- never to claim
# a piece *is* analysis.
ANALYSIS_MARKERS = re.compile(
    r"\b(analysis|opinion|why|how|what|explainer|outlook|forecast|warns?|warning|"
    r"risk|says?|argues?|expects?|sees?|calls?|strategist|economist|comment)\b",
    re.IGNORECASE,
)

# Desks whose framing is worth reading even when the body is paywalled.
PREMIUM_KEYS = ("ft", "wsj", "economist", "reuters", "bloomberg", "barrons", "nikkei", "imf_bis")


def load_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def iter_headlines(payload: dict, buckets: Iterable[str] = ("topics", "wires", "primary", "premium")):
    for bucket in buckets:
        for key, group in (payload.get(bucket) or {}).items():
            for headline in group.get("headlines", []):
                yield bucket, key, group.get("label", key), headline


def _topic_counts(payload: dict) -> dict[str, int]:
    return {key: group.get("count", 0) for key, group in (payload.get("topics") or {}).items()}


def _distinct_days(payload: dict) -> int:
    days = {
        (h.get("published_utc") or "")[:10]
        for _, _, _, h in iter_headlines(payload)
        if h.get("published_utc")
    }
    return max(len(days), 1)


def _topic_day_matrix(payload: dict) -> dict[str, Counter]:
    """{topic: Counter({'YYYY-MM-DD': n})} over the weekly pull, by Pacific
    publication date -- the raw material for a per-topic daily baseline."""
    matrix: dict[str, Counter] = {}
    for bucket, key, _, headline in iter_headlines(payload, buckets=("topics",)):
        stamp = headline.get("published_utc")
        if not stamp:
            continue
        try:
            day = datetime.fromisoformat(stamp).astimezone(timezone.utc).date().isoformat()
        except ValueError:
            continue
        matrix.setdefault(key, Counter())[day] += 1
    return matrix


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def build_attention_shift(daily: dict, weekly: dict, limit: int = 8) -> list[dict]:
    """Today's topic volume vs. that topic's own typical day.

    Ratio, not raw count: some topics simply produce more articles than others
    every day, so an absolute ranking just re-discovers which desks are
    chatty. The ratio asks the only interesting question -- is this topic
    unusually busy *for itself* right now.

    The baseline is the MEDIAN of the topic's prior days, not the mean of the
    whole week. Two reasons, both of which otherwise make everything look
    elevated: a mean over the full window includes today, so today inflates
    its own baseline; and the window spans a weekend, whose near-zero counts
    drag a mean down far enough that every weekday reads as a surge. A median
    over prior days only is robust to both."""
    today_counts = _topic_counts(daily)
    week_counts = _topic_counts(weekly)
    matrix = _topic_day_matrix(weekly)
    today_key = datetime.now(timezone.utc).date().isoformat()

    rows = []
    for key, count in today_counts.items():
        group = daily["topics"][key]
        per_day = matrix.get(key, Counter())
        prior_days = sorted(d for d in per_day if d != today_key)
        baseline = _median([per_day[d] for d in prior_days]) if prior_days else 0.0

        # Guard the ratio so a topic that is normally silent cannot post an
        # enormous multiple off one extra article.
        denominator = max(baseline, 1.0)
        ratio = count / denominator
        rows.append({
            "topic": key,
            "label": group.get("label", key),
            "today": count,
            "week_total": week_counts.get(key, 0),
            "baseline_per_day": round(baseline, 1),
            "baseline_days": len(prior_days),
            "ratio": round(ratio, 2),
            "direction": "up" if ratio >= 1.5 else ("down" if ratio <= 0.6 else "flat"),
            "newest": group.get("newest"),
        })
    rows.sort(key=lambda r: (r["ratio"], r["today"]), reverse=True)
    return rows[:limit]


def _cluster_key(title: str) -> frozenset:
    """Crude content signature: the significant words in a headline. Wire
    stories get rewritten per outlet, so exact-title matching finds almost no
    cross-outlet agreement; shared vocabulary finds it well enough to count
    outlets, which is all this needs to do."""
    stop = {
        "the", "a", "an", "and", "or", "but", "for", "of", "to", "in", "on", "at", "by",
        "with", "from", "as", "is", "are", "was", "were", "be", "been", "it", "its",
        "this", "that", "after", "over", "amid", "says", "said", "will", "has", "have",
        "new", "how", "why", "what", "up", "down", "more", "than", "into", "out",
    }
    words = re.findall(r"[a-z0-9]+", title.lower())
    return frozenset(w for w in words if len(w) > 3 and w not in stop)


def build_corroboration(daily: dict, min_outlets: int = 3, limit: int = 10) -> list[dict]:
    """Story clusters ranked by how many *distinct outlets* carry them.

    This is the brief's "count independent sources, not articles" rule made
    mechanical. It is a floor, not proof of independence: syndication means
    several outlets can still be one wire, which the caveat on the rendered
    panel says plainly."""
    items = []
    for _, _, _, headline in iter_headlines(daily):
        title, source = headline.get("title"), headline.get("source")
        if not title or not source:
            continue
        signature = _cluster_key(title)
        if len(signature) >= 4:
            items.append((signature, source, headline))

    clusters: list[dict] = []
    for signature, source, headline in items:
        for cluster in clusters:
            overlap = len(signature & cluster["signature"])
            if overlap >= 4 and overlap / min(len(signature), len(cluster["signature"])) >= 0.5:
                cluster["members"].append((source, headline))
                cluster["signature"] = cluster["signature"] & signature or cluster["signature"]
                break
        else:
            clusters.append({"signature": signature, "members": [(source, headline)]})

    rows = []
    for cluster in clusters:
        outlets = {source for source, _ in cluster["members"]}
        if len(outlets) < min_outlets:
            continue
        members = [h for _, h in cluster["members"]]
        members.sort(key=lambda h: (h.get("age_hours") is None, h.get("age_hours") or 0.0))
        lead = members[0]
        rows.append({
            "title": lead.get("title"),
            "lead_source": lead.get("source"),
            "lead_published_pt": lead.get("published_pt"),
            "lead_age": lead.get("age_label"),
            "link": lead.get("link"),
            "outlet_count": len(outlets),
            "article_count": len(members),
            "outlets": sorted(outlets)[:8],
            "earliest_pt": min(
                (h.get("published_pt") for h in members if h.get("published_pt")),
                key=lambda _: 0, default=None,
            ),
        })
    rows.sort(key=lambda r: (r["outlet_count"], r["article_count"]), reverse=True)
    return rows[:limit]


def build_playbook_volume(daily: dict, weekly: dict) -> list[dict]:
    """Live news volume behind each playbook theme, today vs. the week."""
    today, week = _topic_counts(daily), _topic_counts(weekly)
    rows = []
    for key, theme in PLAYBOOK_THEMES.items():
        topics = theme["topics"]
        today_n = sum(today.get(t, 0) for t in topics)
        week_n = sum(week.get(t, 0) for t in topics)
        contributors = sorted(
            ({"label": (daily.get("topics", {}).get(t) or {}).get("label", t), "count": today.get(t, 0)}
             for t in topics if today.get(t, 0)),
            key=lambda r: r["count"], reverse=True,
        )
        rows.append({
            "key": key,
            "label": theme["label"],
            "today": today_n,
            "week": week_n,
            "share_today": 0.0,
            "contributors": contributors[:4],
        })
    total_today = sum(r["today"] for r in rows) or 1
    for row in rows:
        row["share_today"] = round(row["today"] / total_today * 100, 1)
    rows.sort(key=lambda r: r["today"], reverse=True)
    return rows


def build_premium_leads(daily: dict, per_desk: int = 3) -> list[dict]:
    """What the paywalled desks are leading with -- their story selection is
    itself a signal, readable without the article bodies."""
    desks = []
    for key in PREMIUM_KEYS:
        group = (daily.get("premium") or {}).get(key)
        if not group or not group.get("headlines"):
            continue
        desks.append({
            "key": key,
            "label": group["label"],
            "count": group["count"],
            "newest": group.get("newest"),
            "headlines": group["headlines"][:per_desk],
        })
    return desks


def build_analysis_reads(daily: dict, limit: int = 12) -> list[dict]:
    """Candidate argument pieces -- headlines whose wording signals a claim
    rather than a report, preferring the premium desks and primary sources.
    Surfaced for reading, never summarized into a fact."""
    scored = []
    for bucket, key, group_label, headline in iter_headlines(daily):
        title = headline.get("title") or ""
        if not ANALYSIS_MARKERS.search(title):
            continue
        weight = {"premium": 3, "primary": 2, "topics": 1, "wires": 1}.get(bucket, 1)
        age = headline.get("age_hours")
        recency = 2 if (age is not None and age <= 12) else 1
        scored.append((weight * recency, {
            "title": title,
            "source": headline.get("source"),
            "published_pt": headline.get("published_pt"),
            "age_label": headline.get("age_label"),
            "link": headline.get("link"),
            "summary": headline.get("summary"),
            "desk": group_label,
            "tier": bucket,
        }))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    seen, rows = set(), []
    for _, row in scored:
        signature = _cluster_key(row["title"])
        if any(len(signature & prior) >= 4 for prior in seen):
            continue
        seen.add(signature)
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def build_front_headlines(daily: dict, limit: int = 12, max_per_source: int = 2) -> list[dict]:
    """Newest across every tier, deduped and capped per outlet, for the
    dashboard's headline strip."""
    pooled = []
    for _, _, group_label, headline in iter_headlines(daily):
        if headline.get("title"):
            pooled.append(dict(headline, desk=group_label))
    pooled.sort(key=lambda h: (h.get("age_hours") is None, h.get("age_hours") or 0.0))

    seen_titles: set[frozenset] = set()
    per_source: Counter = Counter()
    rows = []
    for headline in pooled:
        signature = _cluster_key(headline["title"])
        if any(len(signature & prior) >= 4 for prior in seen_titles):
            continue
        source = headline.get("source") or "unattributed"
        if per_source[source] >= max_per_source:
            continue
        seen_titles.add(signature)
        per_source[source] += 1
        rows.append(headline)
        if len(rows) >= limit:
            break
    return rows


def build_news_digest(data_dir: Path, date_str: str) -> Optional[dict]:
    """Assemble the full digest. Returns None when the news pull hasn't been
    run for this date, so the dashboard degrades to its pre-news layout
    instead of failing."""
    daily = load_json(data_dir / f"{date_str}-news.json")
    if not daily:
        return None
    weekly = load_json(data_dir / f"{date_str}-news-weekly.json") or daily

    return {
        "generated_at_pt": daily.get("generated_at_pt"),
        "daily_window_hours": daily.get("window_hours"),
        "weekly_window_hours": weekly.get("window_hours"),
        "totals": daily.get("totals", {}),
        "weekly_totals": weekly.get("totals", {}),
        "distinct_sources": (daily.get("rollup") or {}).get("distinct_sources"),
        "weekly_distinct_sources": (weekly.get("rollup") or {}).get("distinct_sources"),
        "front_headlines": build_front_headlines(daily),
        "attention": build_attention_shift(daily, weekly),
        "corroboration": build_corroboration(daily),
        "playbook_volume": build_playbook_volume(daily, weekly),
        "premium_leads": build_premium_leads(daily),
        "analysis_reads": build_analysis_reads(daily),
        "by_day": (weekly.get("rollup") or {}).get("by_day", [])[:7],
        "top_sources": (weekly.get("rollup") or {}).get("top_sources", [])[:12],
        "fetch_errors": daily.get("fetch_errors", []),
        "source_counts": daily.get("source_counts", {}),
    }
