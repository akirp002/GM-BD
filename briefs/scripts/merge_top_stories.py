#!/usr/bin/env python3
"""
Fold a researched top-stories draft into that day's narrative.json.

The draft (`<date>-top-stories-draft.json`) is the raw research artifact: one
entry per story with the underlying detail, a dated timeline, named quotes
with the date each was said, and timestamped sources. This script maps it
onto the narrative's `top_stories` list so the dashboard has a single
authoritative input, while the draft stays on disk as provenance.

Backwards compatible: `title`/`body`/`read` keep the shape the old three-card
layout used, so nothing breaks if the richer fields are absent.

Usage:
    python3 merge_top_stories.py <YYYY-MM-DD> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # briefs/
DATA_DIR = ROOT / "data"

# Draft theme keys -> the label shown on the story's playbook tag.
THEME_LABELS = {
    "fiscal_dominance": "Fiscal dominance",
    "structural_inflation": "Structural inflation",
    "geopolitical_fragmentation": "Geopolitical fragmentation",
    "private_credit": "Private-credit liquidity",
    "leverage_basis": "Leverage / basis trade",
    "passive_concentration": "Passive concentration",
}


def merge_story(index: int, story: dict) -> dict:
    """One draft story -> one narrative top_story, richer fields carried over
    verbatim so the renderer decides presentation, not this script."""
    theme = story.get("playbook_theme")
    return {
        "num": f"{index:02d}",
        "id": story.get("id"),
        "title": story.get("title", ""),
        # `body` is the old single-line field the compact card used; the deck
        # is exactly that sentence, so the fallback path stays intact.
        "body": story.get("deck", ""),
        "read": story.get("read", ""),
        "details": story.get("details", []),
        "timeline": story.get("timeline", []),
        "quotes": story.get("quotes", []),
        "sources": story.get("sources", []),
        "disagreement": story.get("disagreement"),
        "playbook_theme": theme,
        "playbook_label": THEME_LABELS.get(theme, theme),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("date")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    draft_path = DATA_DIR / f"{args.date}-top-stories-draft.json"
    narrative_path = DATA_DIR / f"{args.date}-narrative.json"
    for path in (draft_path, narrative_path):
        if not path.exists():
            raise SystemExit(f"Missing {path}")

    draft = json.loads(draft_path.read_text())
    narrative = json.loads(narrative_path.read_text())
    stories = draft.get("stories", [])
    if not stories:
        raise SystemExit(f"No stories in {draft_path}")

    narrative["top_stories"] = [merge_story(i, s) for i, s in enumerate(stories, start=1)]
    # The five enriched stories replace the old 3-card + 2-extra split; an
    # empty list keeps build_news_pulse's `top_stories + top_extra` valid.
    narrative["top_extra"] = []
    narrative["top_stories_notes"] = draft.get("notes", [])
    narrative["top_stories_researched_at"] = draft.get("generated_at")

    counts = {
        "stories": len(narrative["top_stories"]),
        "details": sum(len(s["details"]) for s in narrative["top_stories"]),
        "timeline_events": sum(len(s["timeline"]) for s in narrative["top_stories"]),
        "quotes": sum(len(s["quotes"]) for s in narrative["top_stories"]),
        "sources": sum(len(s["sources"]) for s in narrative["top_stories"]),
    }
    undated_quotes = [
        (s["title"][:40], q.get("name"))
        for s in narrative["top_stories"] for q in s["quotes"] if not q.get("said_on")
    ]

    print(f"Merged {counts['stories']} stories: {counts['details']} detail bullets, "
          f"{counts['timeline_events']} timeline events, {counts['quotes']} quotes, "
          f"{counts['sources']} sources.")
    if undated_quotes:
        print(f"  {len(undated_quotes)} quote(s) with no date (rendered as 'date not established'):")
        for title, name in undated_quotes:
            print(f"    - {name} (in: {title}...)")
    if narrative["top_stories_notes"]:
        print(f"  {len(narrative['top_stories_notes'])} research caveat(s) carried into the narrative.")

    if args.dry_run:
        print("Dry run: narrative.json not written.")
        return 0

    narrative_path.write_text(json.dumps(narrative, indent=2))
    print(f"Wrote {narrative_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
