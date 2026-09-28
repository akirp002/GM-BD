"""Unit tests for the news aggregator's pure logic.

Network fetching is not exercised here -- these cover the transforms that
decide what reaches the page: dedup keying, freshness windowing, noise
filtering, age formatting, and feed parsing across RSS/Atom shapes.

    python3 -m pytest briefs/tests/test_fetch_news.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from fetch_news import (  # noqa: E402
    Headline,
    dedupe,
    humanize_age,
    is_noise,
    is_redundant_summary,
    normalize_title,
    parse_feed,
    sort_newest_first,
    strip_html,
    strip_outlet_suffix,
    within_window,
)

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def make_headline(title: str, age_hours: float | None = 1.0, source: str = "Reuters") -> Headline:
    return Headline(
        title=title, link="https://example.com/a", source=source,
        published_utc=None, published_pt=None, age_hours=age_hours,
        age_label=humanize_age(age_hours), summary=None,
        origin="test", tier="topic", group="g", group_label="G",
    )


class TestNormalizeTitle:
    def test_casefolds_and_strips_punctuation(self):
        assert normalize_title("Fed Holds Rates!") == "fed holds rates"

    def test_collapses_whitespace(self):
        assert normalize_title("  Oil   spikes  ") == "oil spikes"

    def test_matches_across_punctuation_variants(self):
        assert normalize_title("Yields hit 5%") == normalize_title("Yields hit 5 %")


class TestDedupe:
    def test_keeps_first_occurrence(self):
        items = [make_headline("Fed holds", 1.0), make_headline("Fed Holds!", 2.0)]
        assert [h.age_hours for h in dedupe(items)] == [1.0]

    def test_keeps_distinct_titles(self):
        assert len(dedupe([make_headline("A story"), make_headline("B story")])) == 2

    def test_drops_empty_titles(self):
        assert dedupe([make_headline("   ")]) == []


class TestWithinWindow:
    def test_inside_window(self):
        assert within_window(make_headline("x", 10.0), 36)

    def test_outside_window(self):
        assert not within_window(make_headline("x", 48.0), 36)

    def test_boundary_is_inclusive(self):
        assert within_window(make_headline("x", 36.0), 36)

    def test_undated_is_kept(self):
        """Google News omits pubDate on some entries; dropping them would lose
        real stories on an unknowable timestamp."""
        assert within_window(make_headline("x", None), 36)


class TestHumanizeAge:
    @pytest.mark.parametrize("hours,expected", [
        (0.25, "15m ago"), (0.5, "30m ago"), (1.0, "1h ago"),
        (5.4, "5h ago"), (23.6, "24h ago"), (30.0, "1d ago"),
        (100.0, "4d ago"), (200.0, "1w ago"), (None, "undated"),
    ])
    def test_formats(self, hours, expected):
        assert humanize_age(hours) == expected

    def test_sub_minute_floors_to_one_minute(self):
        assert humanize_age(0.001) == "1m ago"


class TestSortNewestFirst:
    def test_newest_first(self):
        items = [make_headline("old", 10.0), make_headline("new", 1.0)]
        assert [h.title for h in sort_newest_first(items)] == ["new", "old"]

    def test_undated_sorts_last(self):
        items = [make_headline("undated", None), make_headline("dated", 50.0)]
        assert [h.title for h in sort_newest_first(items)] == ["dated", "undated"]


class TestIsNoise:
    @pytest.mark.parametrize("title", [
        "WisdomTree US Quality Dividend Growth Fund ETF declares $0.17 monthly dividend",
        "Vanguard S&P Small-Cap 600 Growth ETF declares quarterly distribution of $0.2571",
        "Texas Capital Bancshares stock hits 52-week low at $18.68",
        "Natera options flow shows 12.6:1 put/call ratio near 52-week high",
        "Acme Corp earnings call transcript",
        "Analyst initiates coverage on Widget Inc",
        "I just started dating someone I really like, but he's moving to Mexico",
        "Should I sell my house to pay off debt?",
    ])
    def test_flags_boilerplate_and_advice(self, title):
        assert is_noise(title)

    @pytest.mark.parametrize("title", [
        "Treasury yields rip higher on renewed inflation fear",
        "Fed's Williams says it's reasonable to expect another rate hike by year-end",
        "Oil gains about 3% as Houthis target Saudi Arabia",
        "Global bond selloff rolls on, US 30-year yield at highest since 2004",
        "Dividend investors face a harder market in 2027",
        "ECB holds rates as energy shock persists",
    ])
    def test_keeps_real_news(self, title):
        assert not is_noise(title)


class TestStripOutletSuffix:
    def test_strips_when_tail_matches_source(self):
        assert strip_outlet_suffix("Yields climb - Reuters", "Reuters") == ("Yields climb", "Reuters")

    def test_matches_domain_form_of_source(self):
        title, source = strip_outlet_suffix("Nvidia stock rises - barrons.com", "Barron's")
        assert title == "Nvidia stock rises"

    def test_adopts_tail_as_source_when_absent(self):
        assert strip_outlet_suffix("Oil slips - OilPrice.com", None) == ("Oil slips", "OilPrice.com")

    def test_preserves_headline_dash_when_tail_is_not_the_outlet(self):
        """'Fed holds rates - for now' must keep its tail."""
        assert strip_outlet_suffix("Fed holds rates - for now", "Reuters") == (
            "Fed holds rates - for now", "Reuters",
        )

    def test_no_separator_is_untouched(self):
        assert strip_outlet_suffix("ECB signals tightening", "ECB") == ("ECB signals tightening", "ECB")

    def test_strips_only_the_final_segment(self):
        title, _ = strip_outlet_suffix("Trump - Xi summit ends - CNBC", "CNBC")
        assert title == "Trump - Xi summit ends"


class TestIsRedundantSummary:
    def test_identical_text(self):
        assert is_redundant_summary("Yields climb to 5.1%", "Yields climb to 5.1%")

    def test_summary_is_title_plus_outlet(self):
        assert is_redundant_summary("Yields climb to 5.1% Reuters", "Yields climb to 5.1%")

    def test_empty_summary(self):
        assert is_redundant_summary("", "Yields climb")

    def test_genuine_summary_is_kept(self):
        assert not is_redundant_summary(
            "The 30-year rose 8bp after a weak auction.", "Yields climb to 5.1%"
        )


class TestStripHtml:
    def test_removes_tags_and_entities(self):
        assert strip_html("<p>Yields &amp; spreads</p>") == "Yields spreads"

    def test_truncates_to_limit(self):
        assert len(strip_html("x" * 900, limit=100)) == 100


class TestParseFeed:
    RSS = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <title>Yields climb to 5.1% - Reuters</title>
        <link>https://example.com/1</link>
        <pubDate>Fri, 25 Sep 2026 10:00:00 GMT</pubDate>
        <description>&lt;p&gt;Bond selloff continues&lt;/p&gt;</description>
      </item>
      <item>
        <title>Acme ETF declares $0.10 monthly dividend</title>
        <link>https://example.com/2</link>
        <pubDate>Fri, 25 Sep 2026 09:00:00 GMT</pubDate>
      </item>
    </channel></rss>"""

    ATOM = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>ECB signals further tightening</title>
        <link href="https://example.com/3"/>
        <updated>2026-09-25T08:00:00Z</updated>
      </entry>
    </feed>"""

    def _parse(self, doc: str, limit: int = 10):
        return parse_feed(doc.encode(), NOW, "g", "G", "wire", "origin", limit, fallback_source="Feed")

    def test_parses_rss_and_splits_outlet_from_title(self):
        headlines = self._parse(self.RSS)
        assert headlines[0].title == "Yields climb to 5.1%"
        assert headlines[0].source == "Reuters"

    def test_computes_age_and_summary(self):
        headline = self._parse(self.RSS)[0]
        assert headline.age_hours == pytest.approx(2.0)
        assert headline.summary == "Bond selloff continues"

    def test_drops_noise_items(self):
        assert all("dividend" not in h.title for h in self._parse(self.RSS))

    def test_noise_filtering_precedes_limit(self):
        """A feed opening with boilerplate should still yield its real story."""
        assert len(self._parse(self.RSS, limit=1)) == 1
        assert self._parse(self.RSS, limit=1)[0].title == "Yields climb to 5.1%"

    def test_parses_atom_entries(self):
        headlines = self._parse(self.ATOM)
        assert headlines[0].title == "ECB signals further tightening"
        assert headlines[0].link == "https://example.com/3"

    def test_handles_empty_feed(self):
        assert self._parse('<?xml version="1.0"?><rss><channel></channel></rss>') == []
