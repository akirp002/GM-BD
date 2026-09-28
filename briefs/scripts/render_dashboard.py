#!/usr/bin/env python3
"""
Assemble the HTML dashboard entirely from data files, via a real Jinja2 template:

    briefs/data/<date>-market-data.json   (from fetch_market_data.py)  -> heatmap + macro panel
    briefs/data/<date>-heatmap.csv        (from fetch_market_data.py)  -> heatmap
    briefs/data/<date>-narrative.json     (authored by hand each day)  -> everything else

Nothing about a specific day's numbers or story is hardcoded in the template
(briefs/templates/dashboard.html.j2) itself -- it is pure structure. To
produce a new day's dashboard: write that day's narrative.json (from the news
research done per brief.md's Searches/Signal-preservation rules), run
fetch_market_data.py for the quantitative side, then run this script.

Usage:
    python3 fetch_market_data.py 2026-09-25
    # ...write briefs/data/2026-09-25-narrative.json by hand, from research...
    python3 render_dashboard.py 2026-09-25
"""
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader

from lib_news_digest import build_news_digest
from lib_heatmap import (
    build_heatmap_table_html,
    build_macro_panel_html,
    build_valuation_bars_html,
    build_duration_matrix_html,
    build_credit_curve_html,
    build_yield_change_matrix_html,
    build_credit_change_matrix_html,
    build_yield_deep_dive_html,
    build_credit_deep_dive_html,
    build_market_pulse_html,
    build_global_rate_change_matrix_html,
    build_global_inflation_change_matrix_html,
    build_global_output_growth_matrix_html,
    build_global_output_change_matrix_html,
    build_global_consumption_growth_matrix_html,
    build_labor_market_tiles_html,
    build_payrolls_heatmap_html,
)

ROOT = Path(__file__).resolve().parents[1]  # briefs/
TEMPLATE_DIR = ROOT / "templates"
TEMPLATE_NAME = "dashboard.html.j2"


def compute_curve_geometry(fi_panel: dict, yield_curve: Optional[dict] = None) -> dict:
    """Fill in SVG x/y pixel positions for the yield-curve points from raw
    tenor/yield values, so the template only ever draws pre-computed geometry.

    When the FRED yield-curve history is available, the same tenors three
    months ago are drawn behind today's line as a faint reference. Today's
    curve alone says where rates are; the pair says what the move *was* --
    which is the actual subject of this panel."""
    points = fi_panel["curve_points"]
    axis = fi_panel["curve_axis"]

    prior = _prior_curve_points(points, yield_curve, "3M ago")
    # Widen the axis if the reference curve sits outside today's bounds,
    # otherwise the ghost line clips against the top or floor of the plot.
    all_yields = [p["yield"] for p in points] + [p["yield"] for p in prior]
    lo = min(axis["min"], math.floor(min(all_yields) * 4) / 4)
    hi = max(axis["max"], math.ceil(max(all_yields) * 4) / 4)
    axis["min"], axis["max"] = lo, hi
    axis["mid_label"] = round((lo + hi) / 2, 2)

    n = len(points)
    x_start, x_end = 100, 400
    step = (x_end - x_start) / (n - 1) if n > 1 else 0

    def place(series: list) -> None:
        for i, p in enumerate(series):
            p["x"] = x_start + i * step
            # y=100 at axis min, y=30 at axis max (matches the SVG's fixed gridlines)
            frac = (p["yield"] - lo) / (hi - lo) if hi != lo else 0.5
            p["y"] = round(100 - frac * 70, 1)
            p["label_y"] = max(p["y"] - 7, 20)

    place(points)
    place(prior)

    fi_panel["curve_polyline"] = " ".join(f"{p['x']},{p['y']}" for p in points)
    fi_panel["prior_curve_points"] = prior
    fi_panel["prior_curve_polyline"] = " ".join(f"{p['x']},{p['y']}" for p in prior)
    if prior:
        fi_panel["prior_curve_asof"] = prior[0].get("as_of")
        fi_panel["prior_curve_deltas"] = [
            {
                "tenor": now["tenor"],
                "now": now["yield"],
                "then": then["yield"],
                "bp": round((now["yield"] - then["yield"]) * 100),
            }
            for now, then in zip(points, prior)
        ]
    return fi_panel


def _prior_curve_points(points: list, yield_curve: Optional[dict], label: str) -> list:
    """Today's tenors sampled from a past snapshot of the FRED curve. Returns
    [] unless every tenor on today's curve is present in that snapshot -- a
    partial reference line would imply a curve shape the data doesn't support."""
    if not yield_curve:
        return []
    snapshot = (yield_curve.get("snapshots") or {}).get(label) or {}
    as_of = yield_curve.get("asof")
    prior = []
    for p in points:
        value = snapshot.get(p["tenor"])
        if value is None:
            return []
        prior.append({"tenor": p["tenor"], "yield": float(value), "as_of": as_of})
    return prior


def compute_evolution_geometry(categories: list, snapshots: dict, y_suffix: str) -> dict:
    """Shared multi-line-overlay geometry for the yield-curve and credit-curve
    evolution charts: one polyline per lookback snapshot (1W/1M/3M/1Y/3Y ago,
    Now), all on the same category axis (tenor or rating) and a shared,
    padded value axis. `snapshots` is {label: {category: value_or_None}},
    oldest-to-newest (drawing order: oldest first, "Now" drawn last/on top)."""
    plot_x0, plot_x1 = 60, 440
    plot_y0, plot_y1 = 30, 220
    n = len(categories)
    step = (plot_x1 - plot_x0) / (n - 1) if n > 1 else 0

    all_vals = [v for row in snapshots.values() for v in row.values() if v is not None]
    lo, hi = (min(all_vals), max(all_vals)) if all_vals else (0, 1)
    pad = (hi - lo) * 0.12 or max(abs(hi), 1) * 0.1
    lo, hi = lo - pad, hi + pad

    labels = list(snapshots.keys())
    lines = []
    for i, label in enumerate(labels):
        row = snapshots[label]
        pts = []
        for j, cat in enumerate(categories):
            v = row.get(cat)
            if v is None:
                continue
            x = round(plot_x0 + j * step, 1)
            frac = (v - lo) / (hi - lo) if hi != lo else 0.5
            y = round(plot_y1 - frac * (plot_y1 - plot_y0), 1)
            pts.append({"x": x, "y": y, "cat": cat, "value": v})
        is_now = label == "Now"
        lines.append({
            "label": label,
            "points": pts,
            "polyline": " ".join(f"{p['x']},{p['y']}" for p in pts),
            "is_now": is_now,
            "color": "var(--accent)" if is_now else "var(--ink-faint)",
            "opacity": 1.0 if is_now else round(0.3 + 0.55 * (i / max(len(labels) - 1, 1)), 2),
            "dasharray": "none" if is_now else "4,3",
            "stroke_width": 2.5 if is_now else 1.3,
        })

    # A snapshot with zero points (every tenor/rating None at that lookback --
    # e.g. a series whose FRED history doesn't reach 3 years back) would draw
    # an invisible line with a legend entry pointing at nothing. Drop it from
    # what's drawn, but keep its label so the caller can say so explicitly
    # rather than silently showing a shorter curve than promised.
    drawable = [l for l in lines if l["points"]]
    missing = [l["label"] for l in lines if not l["points"]]

    axis_cats = [{"x": round(plot_x0 + j * step, 1), "label": cat} for j, cat in enumerate(categories)]
    return {
        "lines": drawable,
        "missing_snapshots": missing,
        "categories": axis_cats,
        "bounds": {"x0": plot_x0, "x1": plot_x1, "y0": plot_y0, "y1": plot_y1},
        "y_min": round(lo, 2),
        "y_max": round(hi, 2),
        "y_mid": round((lo + hi) / 2, 2),
        "y_suffix": y_suffix,
    }


def compute_yield_curve_evolution(yield_curve: dict) -> dict:
    geo = compute_evolution_geometry(yield_curve["tenors"], yield_curve["snapshots"], "%")
    geo["asof"] = yield_curve.get("asof")
    geo["history_start"] = yield_curve.get("history_start")
    return geo


def compute_credit_curve_evolution(credit_curve: dict) -> dict:
    flat_snapshots = {
        label: {r: v.get("value_bp") for r, v in row.items()}
        for label, row in credit_curve["snapshots"].items()
    }
    geo = compute_evolution_geometry(credit_curve["ratings"], flat_snapshots, "bp")
    geo["asof"] = credit_curve.get("asof")
    geo["history_start"] = credit_curve.get("history_start")
    return geo


# Fixed categorical palette for the country-trajectory charts below -- unlike
# compute_evolution_geometry's snapshots (oldest faded, "Now" bold/accent),
# every line here is an equally-live country series, so each gets its own
# stable, always-solid color instead of one line being highlighted.
COUNTRY_COLORS = {
    "US": "#4C9AFF", "Euro Area": "#FF8A65", "UK": "#81C784", "Japan": "#BA68C8",
    "Canada": "#F06292", "Australia": "#4DD0E1", "Switzerland": "#FFB74D",
}

def compute_country_history_chart(global_macro: dict, indicator_key: str, y_suffix: str) -> dict:
    """One line per country plotted across its full trailing 36-month
    history (`fetch_global_macro_matrix`'s shared monthly grid) -- real
    monthly resolution rather than the 8 discrete horizon snapshots the
    change/trajectory heatmaps use. Every country's history sits on the
    identical grid (built server-side in fetch_market_data.py), so x
    placement here is just index-into-grid -- no per-line date reconciliation
    needed. Also returns a `countries` list (name + color) so the template
    can render a "focus on one country" selector without hand-authoring it."""
    plot_x0, plot_x1 = 60, 440
    plot_y0, plot_y1 = 30, 220

    # The grid is identical across countries/indicators by construction, but
    # a country with a shorter FRED history (or a fetch failure) may have
    # fewer points -- take the longest available history as the x-axis so a
    # thin series doesn't compress everyone else's timeline.
    histories = {c: ind.get(indicator_key, {}).get("history", []) for c, ind in global_macro.items()}
    all_dates = sorted({p["date"] for h in histories.values() for p in h})
    n = len(all_dates)
    if n == 0:
        return {"lines": [], "missing_snapshots": list(histories.keys()), "categories": [],
                "bounds": {"x0": plot_x0, "x1": plot_x1, "y0": plot_y0, "y1": plot_y1},
                "y_min": 0, "y_max": 1, "y_mid": 0.5, "y_suffix": y_suffix, "countries": []}
    date_index = {d: i for i, d in enumerate(all_dates)}
    step = (plot_x1 - plot_x0) / (n - 1) if n > 1 else 0

    all_vals = [p["value"] for h in histories.values() for p in h]
    lo, hi = (min(all_vals), max(all_vals)) if all_vals else (0, 1)
    pad = (hi - lo) * 0.12 or max(abs(hi), 1) * 0.1
    lo, hi = lo - pad, hi + pad

    lines, countries = [], []
    for country, history in histories.items():
        color = COUNTRY_COLORS.get(country, "var(--ink-faint)")
        meta = global_macro.get(country, {}).get(indicator_key, {})
        pts = []
        for p in history:
            j = date_index[p["date"]]
            x = round(plot_x0 + j * step, 1)
            frac = (p["value"] - lo) / (hi - lo) if hi != lo else 0.5
            y = round(plot_y1 - frac * (plot_y1 - plot_y0), 1)
            pts.append({
                "x": x, "y": y, "cat": p["date"], "value": p["value"],
                # Real prints get a dot; months carried forward between
                # prints do not, so a quarterly series reads as four points a
                # year rather than twelve identical-looking observations.
                "real": not p.get("filled", False),
            })
        lines.append({
            "label": country, "points": pts,
            "polyline": " ".join(f"{p['x']},{p['y']}" for p in pts),
            "color": color, "opacity": 1.0, "dasharray": "none", "stroke_width": 1.8,
            "last_print": meta.get("asof"),
            "months_stale": meta.get("months_stale"),
            "is_stale": meta.get("is_stale", False),
        })
        countries.append({
            "label": country, "color": color,
            "last_print": meta.get("asof"), "is_stale": meta.get("is_stale", False),
            "months_stale": meta.get("months_stale"),
        })

    drawable = [l for l in lines if l["points"]]
    missing = [l["label"] for l in lines if not l["points"]]
    # Sparse x-axis ticks (~6 across the full width) -- printing all 36
    # month labels would collide; hover/point titles still carry every date.
    tick_every = max(n // 6, 1)
    axis_cats = [
        {"x": round(plot_x0 + j * step, 1), "label": d[:7]}
        for j, d in enumerate(all_dates) if j % tick_every == 0 or j == n - 1
    ]
    return {
        "lines": drawable,
        "missing_snapshots": missing,
        "categories": axis_cats,
        "bounds": {"x0": plot_x0, "x1": plot_x1, "y0": plot_y0, "y1": plot_y1},
        "y_min": round(lo, 2),
        "y_max": round(hi, 2),
        "y_mid": round((lo + hi) / 2, 2),
        "y_suffix": y_suffix,
        "countries": countries,
        "span_start": all_dates[0][:7],
        "span_end": all_dates[-1][:7],
        # Each line ends at its own last real print, so the chart's right edge
        # is the freshest series, not "today". Stated explicitly rather than
        # left for the reader to infer from a line that stops early.
        "stale_countries": [c for c in countries if c.get("is_stale")],
        "freshest_print": max((c["last_print"] for c in countries if c.get("last_print")), default=None),
    }


def compute_cb_comparison_bars(cb_matrix: dict) -> dict:
    """Two diverging-bar comparisons alongside the quadrant chart: each
    bank's real rate (policy - CPI), and its policy-rate carry versus the
    Fed -- the FX-carry-relevant number, since USD/JPY-type crosses trade
    off rate differentials, not absolute levels."""
    banks = cb_matrix["banks"]
    fed = next((b for b in banks if b["short"] == "Fed"), banks[0])
    fed_rate = fed["policy_rate_pct"]

    real_cap = max((abs(b["real_rate_pct"]) for b in banks), default=1) or 1
    real_bars = []
    for b in banks:
        rr = b["real_rate_pct"]
        real_bars.append({
            "short": b["short"], "value": rr,
            "pct_width": round(min(abs(rr) / real_cap, 1.0) * 50, 1),
            "side": "right" if rr >= 0 else "left",
            "token": "up" if rr >= 0 else "down",
        })

    carry = [{"short": b["short"], "value": round(b["policy_rate_pct"] - fed_rate, 2)} for b in banks]
    carry_cap = max((abs(c["value"]) for c in carry), default=1) or 1
    for c in carry:
        c["pct_width"] = round(min(abs(c["value"]) / carry_cap, 1.0) * 50, 1)
        c["side"] = "right" if c["value"] >= 0 else "left"
        c["token"] = "down" if c["value"] > 0 else "up"

    cb_matrix["real_rate_bars"] = real_bars
    cb_matrix["carry_vs_fed_bars"] = carry
    return cb_matrix


def compute_cb_quadrant_geometry(cb_matrix: dict) -> dict:
    """Fill in SVG x/y/r pixel positions for the CB-rates quadrant chart
    (x=growth, y=inflation, bubble radius=policy rate) from the raw
    growth/cpi_yoy/policy_rate values authored in the narrative, so the
    template only ever draws pre-computed geometry."""
    banks = cb_matrix["banks"]
    x_axis, y_axis = cb_matrix["growth_axis"], cb_matrix["inflation_axis"]
    plot_x0, plot_x1 = 60, 440
    plot_y0, plot_y1 = 30, 260  # svg y grows downward; y1 is the bottom (low inflation)

    rates = [b["policy_rate_pct"] for b in banks if b.get("policy_rate_pct") is not None]
    r_lo, r_hi = (min(rates), max(rates)) if rates else (0, 1)

    for b in banks:
        gx = (b["growth_pct"] - x_axis["min"]) / (x_axis["max"] - x_axis["min"])
        iy = (b["cpi_yoy_pct"] - y_axis["min"]) / (y_axis["max"] - y_axis["min"])
        b["x"] = round(plot_x0 + gx * (plot_x1 - plot_x0), 1)
        b["y"] = round(plot_y1 - iy * (plot_y1 - plot_y0), 1)
        rate = b.get("policy_rate_pct")
        if rate is None or r_hi == r_lo:
            b["r"] = 16
        else:
            b["r"] = round(10 + (rate - r_lo) / (r_hi - r_lo) * 16, 1)

    cb_matrix["plot_bounds"] = {"x0": plot_x0, "x1": plot_x1, "y0": plot_y0, "y1": plot_y1}
    cb_matrix["mid_x"] = round((plot_x0 + plot_x1) / 2, 1)
    cb_matrix["mid_y"] = round((plot_y0 + plot_y1) / 2, 1)
    return cb_matrix


# Stable per-sector colors for the rotation chart. Hand-picked rather than
# hue-rotated: eleven evenly-rotated hues put several sectors on near-identical
# blues/greens, and this chart is read by tracing one sector's line across
# crossings, which only works if its color is unmistakable.
SECTOR_COLORS = {
    "XLK": "#4C9AFF", "XLF": "#F06292", "XLE": "#FFB74D", "XLV": "#81C784",
    "XLY": "#BA68C8", "XLP": "#4DD0E1", "XLI": "#A1887F", "XLB": "#9575CD",
    "XLU": "#E57373", "XLRE": "#4DB6AC", "XLC": "#FFD54F",
}


def compute_sector_rotation_chart(
    market_data: dict, lookback: int = 3,
    width: float = 430, height: float = 300, x0: float = 52, y0: float = 22,
) -> Optional[dict]:
    """Sector-leadership *rank* over time -- a bump chart, replacing the
    stacked streamgraph that used to sit here.

    The streamgraph was the wrong form for this question. Every sector was
    indexed to 100 at the window start, so the bands were near-identical
    widths and the whole chart read as a flat rainbow slab: the one thing it
    made visually obvious (relative band thickness) was the one thing that
    barely varied. Rotation is an *ordering* phenomenon, so rank is the
    honest encoding -- lines crossing are leadership changes, which is
    precisely what "rotation" means.

    Rank is on trailing `lookback`-month return, not cumulative-since-start:
    a cumulative ranking is dominated by whatever led in month one and barely
    moves afterward, which would hide the rotation this chart exists to show.
    """
    sg = market_data.get("sector_streamgraph") or {}
    series, dates = sg.get("series") or {}, sg.get("dates") or []
    labels = sg.get("labels") or {}
    tickers = [t for t in series if any(v is not None for v in series[t])]
    if len(tickers) < 2 or len(dates) <= lookback:
        return None

    # Trailing-return matrix: r[t] = idx[t]/idx[t-lookback] - 1, in percent.
    periods, ranked = [], []
    for i in range(lookback, len(dates)):
        row = {}
        for ticker in tickers:
            now_val, past_val = series[ticker][i], series[ticker][i - lookback]
            if now_val is None or past_val in (None, 0):
                continue
            row[ticker] = (now_val / past_val - 1) * 100
        if len(row) < 2:
            continue
        periods.append(dates[i])
        order = sorted(row, key=lambda t: row[t], reverse=True)
        ranked.append({t: {"rank": pos + 1, "ret": row[t]} for pos, t in enumerate(order)})

    if not periods:
        return None

    n, n_ranks = len(periods), len(tickers)
    x_step = width / max(n - 1, 1)
    y_step = height / max(n_ranks - 1, 1)
    x_px = lambda i: round(x0 + i * x_step, 1)
    y_px = lambda rank: round(y0 + (rank - 1) * y_step, 1)

    lines = []
    for ticker in tickers:
        points = [
            {"x": x_px(i), "y": y_px(ranked[i][ticker]["rank"]), "cat": periods[i],
             "rank": ranked[i][ticker]["rank"], "ret": round(ranked[i][ticker]["ret"], 1)}
            for i in range(n) if ticker in ranked[i]
        ]
        if not points:
            continue
        final = ranked[-1].get(ticker)
        lines.append({
            "name": ticker,
            "label": labels.get(ticker, ticker),
            "color": SECTOR_COLORS.get(ticker, "var(--ink-faint)"),
            "points": points,
            "polyline": " ".join(f"{p['x']},{p['y']}" for p in points),
            "start_rank": points[0]["rank"],
            "end_rank": final["rank"] if final else None,
            "end_ret": round(final["ret"], 1) if final else None,
            "end_x": points[-1]["x"],
            "end_y": points[-1]["y"],
            "moved": (points[0]["rank"] - final["rank"]) if final else 0,
        })

    lines.sort(key=lambda l: l["end_rank"] or 99)
    tick_every = max(n // 7, 1)
    return {
        "lines": lines,
        "lookback": lookback,
        "n_ranks": n_ranks,
        "x_axis": [{"x": x_px(i), "label": periods[i]} for i in range(n) if i % tick_every == 0 or i == n - 1],
        "rank_axis": [{"y": y_px(r), "label": str(r)} for r in range(1, n_ranks + 1)],
        "bounds": {"x0": x0, "x1": x0 + width, "y0": y0, "y1": y0 + height},
        "span": f"{periods[0]} → {periods[-1]}",
        "leaders": [{"label": l["label"], "ret": l["end_ret"]} for l in lines[:3]],
        "laggards": [{"label": l["label"], "ret": l["end_ret"]} for l in lines[-3:]],
        "biggest_climb": max(lines, key=lambda l: l["moved"]) if lines else None,
        "biggest_fall": min(lines, key=lambda l: l["moved"]) if lines else None,
    }


CREDIT_RATING_COLORS = {
    "AAA": "#4DD0E1", "AA": "#4DB6AC", "A": "#81C784", "BBB": "#FFD54F",
    "BB": "#FFB74D", "B": "#F06292", "CCC": "#E57373",
}


def compute_credit_history_chart(
    market_data: dict, width: float = 430, height: float = 250, x0: float = 56, y0: float = 20,
) -> Optional[dict]:
    """OAS by rating over time as log-scaled lines, replacing the stacked
    credit streamgraph.

    Log scale because the series span two orders of magnitude (AAA ~40bp vs
    CCC ~1100bp): on a linear axis the four investment-grade rungs collapse
    onto the floor and their moves -- the ones that matter for an IG-heavy
    book -- become invisible. On a log axis equal vertical distance is equal
    *proportional* widening, which is how credit risk actually compounds.
    Stacking was also wrong here: these rungs are alternatives, not
    components, so a stacked total has no meaning."""
    stream = ((market_data.get("credit_curve") or {}).get("stream")) or {}
    series, dates = stream.get("series") or {}, stream.get("dates") or []
    ratings = [r for r in (market_data.get("credit_curve", {}).get("ratings") or []) if r in series]
    if not ratings or len(dates) < 2:
        return None

    values = [v for r in ratings for v in series[r] if v is not None and v > 0]
    if not values:
        return None
    lo, hi = min(values), max(values)
    log_lo, log_hi = math.log10(lo * 0.85), math.log10(hi * 1.15)

    n = len(dates)
    x_step = width / max(n - 1, 1)
    x_px = lambda i: round(x0 + i * x_step, 1)

    def y_px(val: float) -> float:
        frac = (math.log10(val) - log_lo) / (log_hi - log_lo) if log_hi != log_lo else 0.5
        return round(y0 + height - frac * height, 1)

    lines = []
    for rating in ratings:
        points = [
            {"x": x_px(i), "y": y_px(v), "cat": dates[i], "value": v}
            for i, v in enumerate(series[rating]) if v is not None and v > 0
        ]
        if not points:
            continue
        lines.append({
            "name": rating, "label": rating,
            "color": CREDIT_RATING_COLORS.get(rating, "var(--ink-faint)"),
            "polyline": " ".join(f"{p['x']},{p['y']}" for p in points),
            "points": points,
            "last_value": points[-1]["value"],
            "end_x": points[-1]["x"], "end_y": points[-1]["y"],
        })

    # Decade gridlines (10/100/1000bp) -- the natural reference marks on a log
    # credit axis, and they label themselves.
    gridlines = []
    decade = 10 ** math.floor(math.log10(lo))
    while decade <= hi * 1.15:
        for mult in (1, 2, 5):
            val = decade * mult
            if lo * 0.85 <= val <= hi * 1.15:
                gridlines.append({"y": y_px(val), "label": f"{int(val)}bp"})
        decade *= 10

    tick_every = max(n // 7, 1)
    return {
        "lines": lines,
        "gridlines": gridlines,
        "x_axis": [{"x": x_px(i), "label": dates[i]} for i in range(n) if i % tick_every == 0 or i == n - 1],
        "bounds": {"x0": x0, "x1": x0 + width, "y0": y0, "y1": y0 + height},
        "span": f"{dates[0]} → {dates[-1]}",
    }


def compute_streamgraph_geometry(
    dates: list, series: dict, layer_order: list, labels: dict = None,
    width: float = 380, height: float = 170, x0: float = 60, y0: float = 20,
) -> dict:
    """Silhouette-offset stacked-area ("streamgraph") geometry: layers stack
    symmetrically around a wiggling zero baseline (baseline(t) = -total(t)/2)
    rather than from y=0 -- that centering is what makes it read as organic
    flow rather than a stacked bar chart. A layer's value at a missing point
    counts as 0 for stacking (its neighbors just close the gap there), the
    standard streamgraph convention. Colors are generated, not hand-picked,
    by rotating hue evenly across the layer count, so this stays fully
    reproducible for any number of layers."""
    labels = labels or {}
    n = len(dates)
    if n == 0 or not layer_order:
        return {"layers": [], "x_axis": [], "bounds": {"x0": x0, "x1": x0 + width, "y0": y0, "y1": y0 + height}}

    def v(layer, i):
        val = series.get(layer, [None] * n)[i]
        return val if val is not None else 0.0

    totals = [sum(v(l, i) for l in layer_order) for i in range(n)]
    baseline = [-t / 2.0 for t in totals]

    tops_bottoms = {l: [] for l in layer_order}
    for i in range(n):
        cum = baseline[i]
        for l in layer_order:
            bottom = cum
            top = cum + v(l, i)
            tops_bottoms[l].append((bottom, top))
            cum = top

    all_y = [y for l in layer_order for pair in tops_bottoms[l] for y in pair]
    y_lo, y_hi = min(all_y), max(all_y)
    pad = (y_hi - y_lo) * 0.05 or 1
    y_lo, y_hi = y_lo - pad, y_hi + pad

    x_step = width / max(n - 1, 1)
    x_px = lambda i: round(x0 + i * x_step, 1)
    y_px = lambda val: round(y0 + height - ((val - y_lo) / (y_hi - y_lo) if y_hi != y_lo else 0.5) * height, 1)

    n_layers = len(layer_order)
    layers = []
    for idx, l in enumerate(layer_order):
        top_pts = [(x_px(i), y_px(tops_bottoms[l][i][1])) for i in range(n)]
        bottom_pts = [(x_px(i), y_px(tops_bottoms[l][i][0])) for i in range(n)][::-1]
        path = "M " + " L ".join(f"{x},{y}" for x, y in top_pts + bottom_pts) + " Z"
        hue = round(idx * 360 / n_layers)
        last_val = next((v for v in reversed(series.get(l, [])) if v is not None), None)
        layers.append({
            "name": l, "label": labels.get(l, l), "path": path,
            "color": f"hsl({hue}, 52%, 52%)", "last_value": last_val,
        })

    tick_every = max(n // 8, 1)
    x_axis = [{"x": x_px(i), "label": dates[i]} for i in range(n) if i % tick_every == 0 or i == n - 1]
    return {"layers": layers, "x_axis": x_axis, "bounds": {"x0": x0, "x1": x0 + width, "y0": y0, "y1": y0 + height}}


def compute_sector_streamgraph(market_data: dict):
    sg = market_data.get("sector_streamgraph")
    if not sg or not sg.get("series"):
        return None
    layer_order = [t for t in sg["series"]]
    return compute_streamgraph_geometry(sg["dates"], sg["series"], layer_order, labels=sg.get("labels"))


def compute_credit_streamgraph(market_data: dict):
    stream = (market_data.get("credit_curve") or {}).get("stream")
    if not stream or not stream.get("series"):
        return None
    layer_order = [r for r in market_data["credit_curve"].get("ratings", []) if r in stream["series"]]
    return compute_streamgraph_geometry(stream["dates"], stream["series"], layer_order)


def build_news_pulse(data_dir: Path, asof_date: str) -> dict:
    """News cadence, built by scanning every day's narrative.json rather than
    hand-copying headlines forward: each day's top_stories/top_extra become
    one dated entry in a running story log, then sliced into "this week"
    (last 7 days) and "this month" (last 30 days). Fully reproducible --
    accumulates automatically as more <date>-narrative.json files exist, no
    per-day authoring beyond writing that day's own top_stories once.
    Today's own stories are still shown via the Top Stories section; this is
    the rollup across the days *before* today."""
    asof = datetime.strptime(asof_date, "%Y-%m-%d").date()
    entries = []
    for f in sorted(data_dir.glob("*-narrative.json")):
        d_str = f.name[: -len("-narrative.json")]
        try:
            d = datetime.strptime(d_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d > asof:
            continue
        try:
            nj = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for s in nj.get("top_stories", []) + nj.get("top_extra", []):
            entries.append({"date": d, "date_label": d.strftime("%b %-d"), "title": s.get("title", "")})
    entries.sort(key=lambda e: e["date"], reverse=True)

    days_covered = len({e["date"] for e in entries})
    return {
        "this_week": [e for e in entries if e["date"] >= asof - timedelta(days=7) and e["date"] < asof],
        "this_month": [e for e in entries if e["date"] >= asof - timedelta(days=30) and e["date"] < asof],
        "days_covered": days_covered,
    }


def render(date_str: str) -> Path:
    data_dir = ROOT / "data"
    heatmap_csv = data_dir / f"{date_str}-heatmap.csv"
    market_json = data_dir / f"{date_str}-market-data.json"
    narrative_json = data_dir / f"{date_str}-narrative.json"

    missing = [p for p in (heatmap_csv, market_json, narrative_json) if not p.exists()]
    if missing:
        names = ", ".join(p.name for p in missing)
        raise SystemExit(
            f"Missing data file(s) for {date_str}: {names}\n"
            f"Run fetch_market_data.py {date_str} for the quant files, "
            f"and write {date_str}-narrative.json by hand from that day's research."
        )

    narrative = json.loads(narrative_json.read_text())
    market_data_early = json.loads(market_json.read_text())
    narrative["fixed_income_panel"] = compute_curve_geometry(
        narrative["fixed_income_panel"], market_data_early.get("yield_curve")
    )
    if "cb_matrix" in narrative:
        narrative["cb_matrix"] = compute_cb_quadrant_geometry(narrative["cb_matrix"])
        narrative["cb_matrix"] = compute_cb_comparison_bars(narrative["cb_matrix"])

    market_data = market_data_early
    heatmap_rows = build_heatmap_table_html(heatmap_csv)
    macro_tiles = build_macro_panel_html(market_json)
    valuation_bars = build_valuation_bars_html(market_json)
    duration_rows = build_duration_matrix_html(market_json)
    credit_curve_bars = build_credit_curve_html(market_json)
    yield_change_matrix = build_yield_change_matrix_html(market_json) if market_data.get("yield_curve") else ""
    credit_change_matrix = build_credit_change_matrix_html(market_json) if market_data.get("credit_curve") else ""
    yield_deep_dive = build_yield_deep_dive_html(market_json) if market_data.get("yield_curve") else ""
    credit_deep_dive = build_credit_deep_dive_html(market_json) if market_data.get("credit_curve") else ""
    market_pulse = build_market_pulse_html(heatmap_csv)
    news_pulse = build_news_pulse(data_dir, date_str)
    has_global_macro = bool(market_data.get("global_macro"))
    global_rate_matrix = build_global_rate_change_matrix_html(market_json) if has_global_macro else ""
    global_inflation_matrix = build_global_inflation_change_matrix_html(market_json) if has_global_macro else ""
    global_output_matrix = build_global_output_growth_matrix_html(market_json) if has_global_macro else ""
    global_output_change_matrix = build_global_output_change_matrix_html(market_json) if has_global_macro else ""
    global_consumption_matrix = build_global_consumption_growth_matrix_html(market_json) if has_global_macro else ""
    global_macro_data = market_data.get("global_macro", {})
    inflation_trajectory = compute_country_history_chart(global_macro_data, "cpi_yoy", "%") if has_global_macro else None
    output_trajectory = compute_country_history_chart(global_macro_data, "output_qoq", "%") if has_global_macro else None
    has_labor_market = bool(market_data.get("labor_market"))
    labor_market_tiles = build_labor_market_tiles_html(market_json) if has_labor_market else ""
    payrolls_heatmap = build_payrolls_heatmap_html(market_json) if has_labor_market else ""
    yield_curve_evolution = (
        compute_yield_curve_evolution(market_data["yield_curve"]) if market_data.get("yield_curve") else None
    )
    credit_curve_evolution = (
        compute_credit_curve_evolution(market_data["credit_curve"]) if market_data.get("credit_curve") else None
    )
    # Rank/bump and log-line charts replace the two streamgraphs; see the
    # rationale in each compute_* docstring.
    sector_rotation = compute_sector_rotation_chart(market_data)
    credit_history = compute_credit_history_chart(market_data)
    # The news digest is optional: when fetch_news.py hasn't been run for this
    # date the dashboard simply renders without those sections.
    news_digest = build_news_digest(data_dir, date_str)

    # Autoescape off: this template's content is authored HTML (bold, italics,
    # links) from a trusted local file, not untrusted user input.
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(TEMPLATE_NAME)

    out_html = template.render(
        heatmap_rows=heatmap_rows,
        macro_tiles=macro_tiles,
        valuation_bars=valuation_bars,
        duration_rows=duration_rows,
        credit_curve_bars=credit_curve_bars,
        yield_change_matrix=yield_change_matrix,
        credit_change_matrix=credit_change_matrix,
        yield_deep_dive=yield_deep_dive,
        credit_deep_dive=credit_deep_dive,
        market_pulse=market_pulse,
        news_pulse=news_pulse,
        has_global_macro=has_global_macro,
        global_rate_matrix=global_rate_matrix,
        global_inflation_matrix=global_inflation_matrix,
        global_output_matrix=global_output_matrix,
        global_output_change_matrix=global_output_change_matrix,
        global_consumption_matrix=global_consumption_matrix,
        inflation_trajectory=inflation_trajectory,
        output_trajectory=output_trajectory,
        has_labor_market=has_labor_market,
        labor_market_tiles=labor_market_tiles,
        payrolls_heatmap=payrolls_heatmap,
        yield_curve_evolution=yield_curve_evolution,
        credit_curve_evolution=credit_curve_evolution,
        sector_rotation=sector_rotation,
        credit_history=credit_history,
        news_digest=news_digest,
        news_digest_date=date_str,
        **narrative,
    )

    out_path = ROOT / f"{date_str}-dashboard.html"
    out_path.write_text(out_html)
    return out_path


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else None
    if not date_str:
        raise SystemExit("Usage: render_dashboard.py <YYYY-MM-DD>")
    out_path = render(date_str)
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
