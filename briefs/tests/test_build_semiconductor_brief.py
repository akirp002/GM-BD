"""Tests for semiconductor-news parsing, market summaries, and HTML safety."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_semiconductor_brief import (  # noqa: E402
    build_context,
    build_market_snapshot,
    dedupe_headlines,
    parse_news_feed,
    render_html,
)


def test_parse_news_feed_reads_atom_fields_and_drops_old_entries():
    now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=5)).isoformat()
    old = (now - timedelta(days=8)).isoformat()
    payload = f"""<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Recent chip story</title><link href="https://example.com/recent"/>
        <published>{recent}</published><source><title>Example News</title></source>
        <summary>Summary text</summary></entry>
      <entry><title>Old chip story</title><published>{old}</published></entry>
      <entry><title>Undated chip story</title></entry>
    </feed>""".encode()

    stories = parse_news_feed(payload, "foundry", now)

    assert [story["title"] for story in stories] == ["Recent chip story", "Undated chip story"]
    assert stories[0]["link"] == "https://example.com/recent"
    assert stories[0]["source"] == "Example News"
    assert stories[0]["category"] == "foundry"
    assert stories[1]["published"] is None


def test_dedupe_headlines_keeps_newest_and_ignores_empty_titles():
    stories = [
        {"title": "Chip capacity rises", "published": "2026-11-06T09:00:00+00:00"},
        {"title": " CHIP capacity rises! ", "published": "2026-11-07T09:00:00+00:00"},
        {"title": "  ", "published": None},
    ]

    assert dedupe_headlines(stories) == [stories[1]]


def test_market_snapshot_reports_returns_and_breadth():
    index = pd.date_range("2026-08-01", periods=70, freq="B")
    close = pd.DataFrame({
        "SOXX": [100 + i for i in range(70)],
        "NVDA": [100 + 2 * i for i in range(70)],
        "AMD": [240 - i for i in range(70)],
    }, index=index)
    volume = pd.DataFrame(1_000_000, index=index, columns=close.columns)
    snapshot = build_market_snapshot({"Close": close, "Volume": volume})

    assert snapshot["tickers"]["SOXX"]["returns_pct"]["1W"] > 0
    assert snapshot["tickers"]["NVDA"]["avg_dollar_volume_20d"] > 0
    assert snapshot["breadth_1w"] == {
        "positive": 1,
        "observed": 2,
        "median_return_pct": round(
            (snapshot["tickers"]["NVDA"]["returns_pct"]["1W"]
             + snapshot["tickers"]["AMD"]["returns_pct"]["1W"]) / 2,
            2,
        ),
    }


def test_render_html_escapes_headline_text_and_rejects_unsafe_links():
    data = {
        "generated_at": "2026-11-07T12:00:00+00:00",
        "news": {
            "cycle": [{
                "title": '<script>alert("x")</script>',
                "link": "javascript:alert(1)",
                "source": "Test",
                "published": None,
                "summary": None,
                "category": "cycle",
            }],
            "all": [{"title": "test"}],
        },
        "market": {
            "as_of": None,
            "tickers": {},
            "breadth_1w": {"positive": 0, "observed": 0, "median_return_pct": None},
            "measurement_note": "No data.",
        },
    }
    research = {"top_stories": [], "watchlist": [], "learning_notes": []}
    knowledge = {
        "title": "Semiconductor brief",
        "description": "Research reference",
        "source_hierarchy": [],
        "chain_stages": [],
        "market_mechanics": [],
        "glossary": [],
    }

    html = render_html(build_context("2026-11-07", data, research, knowledge))

    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "href=\"javascript:alert(1)\"" not in html
    assert "Semiconductor brief" in html
