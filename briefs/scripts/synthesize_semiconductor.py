#!/usr/bin/env python3
"""Turn the semiconductor data pull into a grounded, multi-horizon synthesis.

Usage:
    python3 briefs/scripts/synthesize_semiconductor.py [YYYY-MM-DD]
        Build the evidence packet + prompt, call the Claude API (needs
        ANTHROPIC_API_KEY and `pip install anthropic`), validate, and write
        briefs/data/<date>-semiconductor-research.json.
    ... --packet-only          Write <date>-semiconductor-evidence.json and
                               <date>-semiconductor-prompt.md, then stop. Use
                               this to have a Claude Code session do the
                               synthesis, then ingest it with --ingest.
    ... --ingest FILE.json     Validate a synthesis written elsewhere and
                               install it as the dated research file.
    ... --model MODEL          Override the model (default: SEMI_BRIEF_MODEL or
                               claude-opus-5-5).

Design: the model is handed *computed* evidence (multi-horizon returns,
cross-segment correlations and their shifts, rates/credit moves, headlines in
week/month/quarter windows with stable ids, and last period's theses). Every
claim must cite headline ids or named data fields; the validator rejects ids
that are not in the packet, so a link can never be invented, and it flags
themes with no falsifier or no counter-evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib_semi_analytics as sa  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
THESIS_LOG = ROOT / "semiconductors" / "thesis_log.jsonl"
DEFAULT_MODEL = os.environ.get("SEMI_BRIEF_MODEL", "claude-opus-5-5")
CAPS = {"week": 10, "month": 6, "quarter": 5}  # headlines per desk per window
ARCHIVE_CAP = 30
CONFIDENCE = {"high", "medium", "low"}

SYSTEM = """You are the senior semiconductor strategist on a buy-side research desk. \
You write for a portfolio manager who already knows what a wafer is and wants \
judgement, not a news digest. You are handed an evidence packet: computed market \
statistics, rates and credit data, last period's theses, and headlines grouped by \
look-back window (week / month / quarter) with ids.

Standards:
1. SYNTHESIZE, don't list. A theme must connect at least two independent pieces of \
evidence (e.g. a headline plus a price/correlation/credit fact) and say what follows \
from them. Never produce a headline recap. A headline alone is a lead, not proof: \
say when a claim rests on a single outlet or an unconfirmed report.
2. Work across horizons. For each of week, month and quarter say what the dominant \
story is, then say what CHANGED: what is accelerating, fading, or newly contradicted \
versus the longer window. A trend needs the longer window to confirm it; a week that \
contradicts the quarter is the interesting case, so say so.
3. Use the numbers. Cite segment returns, relative strength vs SOXX, correlation level \
and shifts, dispersion, drawdown, yields, real yields, OAS by rating, and the \
semis-vs-credit links. Interpret them: a rising cross-segment correlation means a \
common factor is driving the group; falling means stock-picking. Equipment leading \
or lagging memory/AI compute says something about where in the capex cycle the market \
thinks we are. Segment indexes are equal-weighted; SOXX/SMH are cap-weighted; the gap \
between them is itself information.
4. Tie in rates and credit explicitly: real yields vs long-duration semis multiples, \
HY/CCC OAS as risk-appetite and funding-cost signal, BBB/BB OAS for the issuers that \
actually fund fabs, the curve. Spread-implied loss is a stress gauge, not a default \
rate; bank delinquency is not bond default. Say what is NOT known.
5. Be falsifiable and honest. Every theme needs the strongest counter-evidence in the \
packet, a confidence level you can defend, and concrete falsifiers (observable, dated \
where possible). Do not infer investor intent from price/volume. Distinguish orders from \
revenue, announced capex from capacity, correlation from causation. If the packet lacks \
support for a conclusion, say it is unsupported rather than filling the gap from memory; \
anything from your own background knowledge must be labelled as such.
6. Continuity: compare with prior_theses. For each, state whether the new evidence \
strengthens, weakens or retires it (put that in change_vs_prior). Retire stale theses.
7. Cite using ONLY ids present in the packet (e.g. "H3f9a1c2") in source_ids. Data \
citations go in the `data` string as the field you used (e.g. "segments.memory.returns_pct.3M"). \
Never invent ids, URLs, quotes, or numbers.

Output a single JSON object, no prose outside it, with exactly this shape:
{
 "bottom_line": "3-5 sentences: the one thing a PM must know, why, and what would change your mind",
 "horizons": {
  "week":    {"headline": "...", "synthesis": "one rich paragraph", "what_changed": ["..."], "source_ids": []},
  "month":   {"headline": "...", "synthesis": "...", "what_changed": ["..."], "source_ids": []},
  "quarter": {"headline": "...", "synthesis": "...", "what_changed": ["..."], "source_ids": []}
 },
 "themes": [  // 3-5, ranked by importance
  {"title": "claim-style title", "horizon": "week|month|quarter|multi", "confidence": "high|medium|low",
   "thesis": "the argument in 3-6 sentences, mechanism first",
   "evidence": [{"claim": "what the evidence shows", "data": "packet field(s) + the value", "source_ids": []}],
   "counter_evidence": "strongest evidence against, with source_ids or data",
   "cross_asset": "how rates/credit/other segments confirm or contradict",
   "implication": "what this means for positioning/analysis (not a trade call)",
   "falsifiers": ["observable thing that would prove this wrong"],
   "change_vs_prior": "vs prior_theses, or 'new'",
   "source_ids": []}
 ],
 "industry_linkages": [  // 2-4 read-throughs on how segments are interconnected
  {"title": "...", "segments": ["segment ids"], "read": "causal chain + what the correlation/relative-strength data says", "source_ids": []}
 ],
 "rates_credit": {"read": "paragraph", "semis_link": "how credit/rates co-move with semis, with the correlation values", "unknowns": ["..."]},
 "open_questions": ["questions the evidence cannot yet answer, with what data would answer them"],
 "watchlist": [{"signal": "...", "why": "what it confirms/falsifies", "url": null}],
 "learning_notes": [{"concept": "...", "explanation": "a durable mechanism the PM should retain"}]
}"""


# ---------------------------------------------------------------- packet ----

def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _headline(story: dict[str, Any]) -> dict[str, Any]:
    out = {"id": story["id"], "date": (story.get("published") or "")[:10] or None,
           "source": story.get("source"), "title": story["title"], "desk": story.get("category")}
    summary = (story.get("summary") or "").strip()
    summary = re.sub(r"<[^>]+>", " ", summary)
    summary = re.sub(r"\s+", " ", summary).strip()
    if summary and summary.casefold()[:40] != story["title"].casefold()[:40]:
        out["summary"] = summary[:220]
    return out


def select_headlines(news: dict[str, Any], desks: list[str]) -> dict[str, dict[str, list]]:
    """Per desk and window, newest first and capped, so no window is crowded out."""
    out: dict[str, dict[str, list]] = {}
    for desk in desks:
        buckets: dict[str, list] = {"week": [], "month": [], "quarter": []}
        for story in news.get(desk, []):
            window = story.get("window", "week")
            if len(buckets[window]) < CAPS[window]:
                buckets[window].append(_headline(story))
        out[desk] = buckets
    return out


def _movers(tickers: dict[str, Any], period: str, n: int = 4) -> dict[str, list]:
    rows = [(t, r["returns_pct"].get(period)) for t, r in tickers.items() if r["group"] not in {"ETF", "Benchmark"}]
    rows = sorted([(t, v) for t, v in rows if v is not None], key=lambda x: x[1])
    fmt = lambda xs: [{"ticker": t, "name": sa.NAMES.get(t, t), "pct": v} for t, v in xs]  # noqa: E731
    return {"leaders": fmt(rows[::-1][:n]), "laggards": fmt(rows[:n])}


def build_packet(data: dict[str, Any], as_of: str, prior: list[dict[str, Any]]) -> dict[str, Any]:
    market = data.get("market", {})
    analytics = market.get("analytics", {})
    macro = data.get("macro", {})
    news = data.get("news", {})
    desks = [k for k in news if k not in {"all", "archive", "window_counts"}]
    series = macro.get("series", {})
    macro_compact = {
        sid: {k: row.get(k) for k in ("label", "group", "last", "as_of", "change", "change_unit",
                                       "range_1y", "percentile_1y")}
        for sid, row in series.items()
    }
    return {
        "as_of": as_of,
        "market_as_of": market.get("as_of"),
        "how_to_read": {
            "segments": "Equal-weight daily-rebalanced indexes of the listed members; returns_pct is price return over 1W/1M/3M/6M/YTD/1Y sessions.",
            "vs_bench_*": "Segment return minus SOXX (cap-weighted) in percentage points; beta is 60d vs SOXX.",
            "correlation": "Pearson correlation of daily segment returns over 20/60/120 sessions; correlation_shifts_60d compares the latest 60d window with the prior 60d.",
            "change units": "Yields/OAS changes are basis points; bank-rate changes are percentage points; VIX/dollar are percent.",
            "headline windows": "week = last 7 days; month = 8-30 days ago; quarter = 31-95 days ago; older = archive. The feed is a capped sample: the counts per window are NOT comparable (the windows use different numbers of queries), so never infer attention trends from how many headlines a window has; judge what the stories say.",
        },
        "segments": analytics.get("segments", {}),
        "etfs": {t: market.get("tickers", {}).get(t, {}).get("returns_pct") for t in analytics.get("etfs", [])},
        "benchmarks": {t: market.get("tickers", {}).get(t, {}).get("returns_pct") for t in analytics.get("benchmarks", [])},
        "movers": {p: _movers(market.get("tickers", {}), p) for p in ("1W", "1M", "3M")},
        "breadth_1w": market.get("breadth_1w"),
        "dispersion_1m": analytics.get("dispersion_1m"),
        "dispersion_3m": analytics.get("dispersion_3m"),
        "soxx_drawdown": analytics.get("soxx_drawdown"),
        "correlation": {w: {"top_pairs": v["pairs"]} for w, v in analytics.get("correlation", {}).items()},
        "correlation_60d_matrix": analytics.get("correlation", {}).get("60", {}).get("matrix"),
        "correlation_shifts_60d": analytics.get("correlation_shifts_60d"),
        "rates_credit": macro_compact,
        "hy_implied_loss": macro.get("hy_implied_loss"),
        "semis_credit_links": macro.get("semis_credit_links"),
        "manual_default_rates": macro.get("manual_default_rates"),
        "headlines": select_headlines(news, desks),
        "older_archive_headlines": [_headline({**r, "category": r.get("category")}) for r in news.get("archive", [])[:ARCHIVE_CAP]],
        "prior_theses": prior,
        "data_gaps": data.get("fetch_errors", []),
    }


def packet_ids(packet: dict[str, Any]) -> set[str]:
    ids = {h["id"] for d in packet["headlines"].values() for w in d.values() for h in w}
    ids |= {h["id"] for h in packet["older_archive_headlines"]}
    return ids


def load_prior(as_of: str, n: int = 6) -> list[dict[str, Any]]:
    if not THESIS_LOG.exists():
        return []
    rows = []
    for line in THESIS_LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("as_of", "") < as_of:
                rows.append(row)
    return rows[-n:]


def append_thesis_log(research: dict[str, Any], as_of: str) -> None:
    rows = []
    if THESIS_LOG.exists():
        rows = [line for line in THESIS_LOG.read_text(encoding="utf-8").splitlines()
                if line.strip() and json.loads(line).get("as_of") != as_of]
    entry = {
        "as_of": as_of, "bottom_line": research.get("bottom_line"),
        "themes": [{"title": t.get("title"), "thesis": t.get("thesis"), "confidence": t.get("confidence"),
                    "falsifiers": t.get("falsifiers")} for t in research.get("themes", [])],
    }
    rows.append(json.dumps(entry, ensure_ascii=False))
    THESIS_LOG.parent.mkdir(exist_ok=True)
    THESIS_LOG.write_text("\n".join(rows) + "\n", encoding="utf-8")


# ------------------------------------------------------------ validation ----

def validate_synthesis(obj: dict[str, Any], valid_ids: set[str]) -> tuple[dict[str, Any], list[str]]:
    """Strip uncitable ids and report structural/analytical weaknesses.

    Returns (cleaned, problems). Unknown ids are removed (never kept as links);
    problems are warnings the operator should read before trusting the page.
    """
    problems: list[str] = []

    def clean(ids: Any, where: str) -> list[str]:
        ids = ids if isinstance(ids, list) else []
        bad = [i for i in ids if i not in valid_ids]
        if bad:
            problems.append(f"{where}: removed unknown source ids {bad}")
        return [i for i in ids if i in valid_ids]

    for key in ("bottom_line", "horizons", "themes", "rates_credit"):
        if not obj.get(key):
            problems.append(f"missing required section: {key}")
    horizons = obj.get("horizons") or {}
    for name in ("week", "month", "quarter"):
        h = horizons.get(name)
        if not isinstance(h, dict) or not h.get("synthesis"):
            problems.append(f"horizons.{name}: missing synthesis")
            continue
        h["source_ids"] = clean(h.get("source_ids"), f"horizons.{name}")
    for i, theme in enumerate(obj.get("themes") or []):
        where = f"themes[{i}] '{str(theme.get('title'))[:40]}'"
        theme["source_ids"] = clean(theme.get("source_ids"), where)
        if theme.get("confidence") not in CONFIDENCE:
            problems.append(f"{where}: confidence must be one of {sorted(CONFIDENCE)}")
        if not theme.get("falsifiers"):
            problems.append(f"{where}: no falsifiers")
        if not theme.get("counter_evidence"):
            problems.append(f"{where}: no counter-evidence")
        evidence = theme.get("evidence") or []
        if len(evidence) < 2:
            problems.append(f"{where}: fewer than two evidence items (not a synthesis)")
        for j, ev in enumerate(evidence):
            ev["source_ids"] = clean(ev.get("source_ids"), f"{where}.evidence[{j}]")
            if not ev.get("source_ids") and not ev.get("data"):
                problems.append(f"{where}.evidence[{j}]: neither headline ids nor data cited")
    for i, link in enumerate(obj.get("industry_linkages") or []):
        link["source_ids"] = clean(link.get("source_ids"), f"industry_linkages[{i}]")
    obj.setdefault("watchlist", [])
    obj.setdefault("learning_notes", [])
    return obj, problems


# ------------------------------------------------------------------- LLM ----

def build_user_message(packet: dict[str, Any]) -> str:
    return ("Evidence packet (JSON). Produce the synthesis JSON described in the system prompt.\n\n"
            + json.dumps(packet, ensure_ascii=False, separators=(",", ":")))


def extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        text = text[text.index("{"): text.rindex("}") + 1]
    return json.loads(text)


def call_model(packet: dict[str, Any], model: str) -> dict[str, Any]:
    try:
        import anthropic
    except ImportError as error:  # pragma: no cover
        raise SystemExit("Install the SDK (`pip install anthropic`) or use --packet-only.") from error
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set. Use --packet-only to synthesize in Claude Code instead.")
    client = anthropic.Anthropic()
    with client.messages.stream(
        model=model, max_tokens=16000, system=SYSTEM,
        messages=[{"role": "user", "content": build_user_message(packet)}],
    ) as stream:
        message = stream.get_final_message()
    text = "".join(block.text for block in message.content if block.type == "text")
    return extract_json(text)


def install(research: dict[str, Any], problems: list[str], as_of: str, model: str) -> Path:
    research.update({"as_of": as_of, "schema": 2, "model": model, "validation_warnings": problems})
    path = DATA_DIR / f"{as_of}-semiconductor-research.json"
    path.write_text(json.dumps(research, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    append_thesis_log(research, as_of)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("date", nargs="?", default=str(date.today()))
    parser.add_argument("--packet-only", action="store_true")
    parser.add_argument("--ingest", metavar="FILE")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    as_of = date.fromisoformat(args.date).isoformat()

    data = _load(DATA_DIR / f"{as_of}-semiconductor-data.json")
    packet = build_packet(data, as_of, load_prior(as_of))
    valid = packet_ids(packet)

    if args.packet_only or not args.ingest:
        (DATA_DIR / f"{as_of}-semiconductor-evidence.json").write_text(
            json.dumps(packet, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        (DATA_DIR / f"{as_of}-semiconductor-prompt.md").write_text(
            "# SYSTEM\n\n" + SYSTEM + "\n\n# USER\n\nSee " + f"{as_of}-semiconductor-evidence.json" + "\n", encoding="utf-8")
        print(f"Evidence packet: {len(valid)} citable headlines, "
              f"~{len(json.dumps(packet)) // 4:,} tokens -> {DATA_DIR / f'{as_of}-semiconductor-evidence.json'}")
        if args.packet_only:
            return 0

    if args.ingest:
        raw = extract_json(Path(args.ingest).read_text(encoding="utf-8"))
        model = "claude-code-session"
    else:
        raw = call_model(packet, args.model)
        model = args.model
    research, problems = validate_synthesis(raw, valid)
    path = install(research, problems, as_of, model)
    print(f"Wrote {path}")
    for problem in problems:
        print(f"Validation warning: {problem}", file=sys.stderr)
    print("Now render: python3 briefs/scripts/build_semiconductor_brief.py", as_of, "--render-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
