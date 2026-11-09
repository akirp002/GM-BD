"""Pure analytics for the semiconductor brief (no network, no Jinja).

Everything here takes pandas objects / plain dicts so it can be unit-tested and
reused by the builder, the evidence-packet writer and the synthesis validator.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

# Sub-industry segments. Equal-weighted so one mega-cap does not hide the rest
# of the segment; cap-weighted ETFs (SOXX/SMH) are tracked separately.
SEGMENTS: dict[str, dict[str, Any]] = {
    "ai_compute": {"label": "AI compute & custom silicon", "tickers": ["NVDA", "AMD", "AVGO", "MRVL"]},
    "memory": {"label": "Memory & HBM", "tickers": ["MU", "005930.KS", "000660.KS"]},
    "foundry": {"label": "Foundry & IDM", "tickers": ["TSM", "GFS", "INTC", "UMC"]},
    "equipment": {"label": "Wafer-fab equipment", "tickers": ["ASML", "AMAT", "LRCX", "KLAC"]},
    "test_pack": {"label": "Test, packaging & materials", "tickers": ["TER", "AMKR", "ENTG", "ONTO"]},
    "interconnect": {"label": "AI interconnect & optical", "tickers": ["CRDO", "ALAB", "COHR"]},
    "analog_power": {"label": "Analog, power & auto/industrial", "tickers": ["TXN", "ADI", "ON", "MCHP", "NXPI", "MPWR"]},
    "mobile_rf": {"label": "Mobile, RF & connectivity", "tickers": ["QCOM", "SWKS", "QRVO"]},
    "eda_ip": {"label": "EDA & IP", "tickers": ["SNPS", "CDNS", "ARM"]},
}
NAMES = {
    "NVDA": "NVIDIA", "AMD": "AMD", "AVGO": "Broadcom", "MRVL": "Marvell", "MU": "Micron",
    "005930.KS": "Samsung Electronics", "000660.KS": "SK hynix", "TSM": "TSMC", "GFS": "GlobalFoundries",
    "INTC": "Intel", "UMC": "UMC", "ASML": "ASML", "AMAT": "Applied Materials", "LRCX": "Lam Research",
    "KLAC": "KLA", "TER": "Teradyne", "AMKR": "Amkor", "ENTG": "Entegris", "ONTO": "Onto Innovation",
    "CRDO": "Credo", "ALAB": "Astera Labs", "COHR": "Coherent", "TXN": "Texas Instruments",
    "ADI": "Analog Devices", "ON": "onsemi", "MCHP": "Microchip", "NXPI": "NXP", "MPWR": "Monolithic Power",
    "QCOM": "Qualcomm", "SWKS": "Skyworks", "QRVO": "Qorvo", "SNPS": "Synopsys", "CDNS": "Cadence",
    "ARM": "Arm Holdings", "SOXX": "iShares Semiconductor ETF", "SMH": "VanEck Semiconductor ETF",
    "XSD": "SPDR S&P Semiconductor ETF", "SPY": "S&P 500", "QQQ": "Nasdaq 100",
}
ETFS = ["SOXX", "SMH", "XSD"]
BENCHMARKS = ["SPY", "QQQ"]
HORIZONS: tuple[tuple[str, int | None], ...] = (
    ("1W", 5), ("1M", 21), ("3M", 63), ("6M", 126), ("YTD", None), ("1Y", 252),
)

# FRED series (public fredgraph.csv, no key). unit: "pct" yields/spreads in
# percent (changes shown in bp), "idx" level index, "pct_level" quarterly rates.
MACRO_SERIES: dict[str, dict[str, str]] = {
    "DGS2": {"label": "2Y Treasury yield", "group": "Rates", "unit": "pct"},
    "DGS10": {"label": "10Y Treasury yield", "group": "Rates", "unit": "pct"},
    "T10Y2Y": {"label": "10Y-2Y curve", "group": "Rates", "unit": "pct"},
    "DFII10": {"label": "10Y real yield (TIPS)", "group": "Rates", "unit": "pct"},
    "T10YIE": {"label": "10Y breakeven inflation", "group": "Rates", "unit": "pct"},
    "BAMLC0A0CM": {"label": "IG OAS", "group": "Credit spreads", "unit": "pct"},
    "BAMLC0A4CBBB": {"label": "BBB OAS", "group": "Credit spreads", "unit": "pct"},
    "BAMLH0A0HYM2": {"label": "HY OAS", "group": "Credit spreads", "unit": "pct"},
    "BAMLH0A1HYBB": {"label": "BB OAS", "group": "Credit spreads", "unit": "pct"},
    "BAMLH0A2HYB": {"label": "B OAS", "group": "Credit spreads", "unit": "pct"},
    "BAMLH0A3HYC": {"label": "CCC OAS", "group": "Credit spreads", "unit": "pct"},
    "VIXCLS": {"label": "VIX", "group": "Risk", "unit": "idx"},
    "DTWEXBGS": {"label": "Broad dollar index", "group": "Risk", "unit": "idx"},
}
# Quarterly Fed bank-loan performance series: the only free, keyless default
# proxies on FRED. They are bank-loan delinquency / charge-off rates, NOT
# rated-bond default rates (Moody's / S&P / Fitch are licensed; see
# briefs/semiconductors/default_rates.json for an optional manual override).
CREDIT_QUALITY_SERIES: dict[str, dict[str, str]] = {
    "DRBLACBS": {"label": "Business-loan delinquency rate (banks)", "unit": "pct_level"},
    "CORBLACBS": {"label": "Business-loan charge-off rate (banks, ann.)", "unit": "pct_level"},
    "DRCCLACBS": {"label": "Credit-card delinquency rate (banks)", "unit": "pct_level"},
}
# Assumed recovery for the spread-implied loss proxy: OAS / (1 - recovery).
ASSUMED_RECOVERY = 0.40


def all_segment_tickers() -> list[str]:
    seen: list[str] = []
    for seg in SEGMENTS.values():
        for t in seg["tickers"]:
            if t not in seen:
                seen.append(t)
    return seen


def download_universe() -> list[str]:
    return all_segment_tickers() + ETFS + BENCHMARKS


def pct_return(series: pd.Series, sessions: int | None) -> float | None:
    s = series.dropna()
    if s.empty:
        return None
    if sessions is None:
        same_year = s[s.index.year == s.index[-1].year]
        # Base on the last close of the prior year when available (true YTD).
        prior = s[s.index.year < s.index[-1].year]
        base = float(prior.iloc[-1]) if len(prior) else float(same_year.iloc[0])
    else:
        if len(s) <= sessions:
            return None
        base = float(s.iloc[-sessions - 1])
    if base == 0:
        return None
    return round((float(s.iloc[-1]) / base - 1) * 100, 2)


def returns_table(series: pd.Series) -> dict[str, float | None]:
    return {name: pct_return(series, n) for name, n in HORIZONS}


def segment_indexes(close: pd.DataFrame, min_members: int = 2) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """Equal-weight daily-rebalanced index (base 100) for each segment.

    A day's segment return is the mean of the members' simple returns that have
    a price on both days. Segments with fewer than ``min_members`` usable
    tickers are skipped rather than dressed up as a segment.
    """
    rets = close.pct_change(fill_method=None)
    out: dict[str, pd.Series] = {}
    members: dict[str, list[str]] = {}
    for sid, seg in SEGMENTS.items():
        cols = [t for t in seg["tickers"] if t in rets.columns and rets[t].notna().sum() > 20]
        if len(cols) < min_members:
            continue
        seg_ret = rets[cols].mean(axis=1, skipna=True).fillna(0.0)
        out[sid] = (1 + seg_ret).cumprod() * 100
        members[sid] = cols
    return pd.DataFrame(out), members


def correlation_matrix(returns: pd.DataFrame, window: int | None, lag_end: int = 0) -> pd.DataFrame:
    """Pearson correlation of daily returns over the last ``window`` sessions,
    optionally ending ``lag_end`` sessions before the latest one."""
    r = returns.dropna(how="all")
    if lag_end:
        r = r.iloc[:-lag_end]
    if window:
        r = r.tail(window)
    return r.corr(min_periods=max(10, int((window or 30) * 0.6)))


def matrix_to_dict(m: pd.DataFrame) -> dict[str, dict[str, float | None]]:
    return {
        a: {b: (None if pd.isna(v) else round(float(v), 2)) for b, v in row.items()}
        for a, row in m.iterrows()
    }


def top_pairs(m: pd.DataFrame, n: int = 5) -> dict[str, list[dict[str, Any]]]:
    pairs = []
    cols = list(m.columns)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            v = m.loc[a, b]
            if not pd.isna(v):
                pairs.append({"a": a, "b": b, "corr": round(float(v), 2)})
    pairs.sort(key=lambda p: p["corr"])
    return {"lowest": pairs[:n], "highest": pairs[::-1][:n]}


def correlation_shifts(returns: pd.DataFrame, window: int = 60, n: int = 5) -> list[dict[str, Any]]:
    """Pairs whose correlation moved most between the latest window and the
    window before it: a rising-correlation regime means the group is trading on
    one common factor; falling means dispersion/idiosyncratic drivers."""
    now = correlation_matrix(returns, window)
    before = correlation_matrix(returns, window, lag_end=window)
    shifts = []
    cols = list(now.columns)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            c1, c0 = now.loc[a, b], before.loc[a, b] if a in before.index and b in before.columns else float("nan")
            if not (pd.isna(c1) or pd.isna(c0)):
                shifts.append({"a": a, "b": b, "now": round(float(c1), 2),
                               "prior": round(float(c0), 2), "change": round(float(c1 - c0), 2)})
    shifts.sort(key=lambda s: abs(s["change"]), reverse=True)
    return shifts[:n]


def beta_and_relative(seg_idx: pd.DataFrame, bench: pd.Series, window: int = 60) -> dict[str, dict[str, float | None]]:
    """Beta to a benchmark (e.g. SOXX) and return spread vs it over windows."""
    r = seg_idx.pct_change(fill_method=None)
    b = bench.pct_change(fill_method=None)
    out: dict[str, dict[str, float | None]] = {}
    for col in seg_idx.columns:
        joined = pd.concat([r[col], b], axis=1, keys=["s", "b"]).dropna().tail(window)
        beta = None
        if len(joined) >= 20 and joined["b"].var() > 0:
            beta = round(float(joined["s"].cov(joined["b"]) / joined["b"].var()), 2)
        spread = {}
        for name, n in HORIZONS:
            a1, a2 = pct_return(seg_idx[col], n), pct_return(bench, n)
            spread[name] = None if a1 is None or a2 is None else round(a1 - a2, 2)
        out[col] = {"beta": beta, **{f"vs_bench_{k}": v for k, v in spread.items()}}
    return out


def dispersion(close: pd.DataFrame, tickers: list[str], sessions: int = 21) -> dict[str, Any]:
    """Cross-sectional spread of trailing returns: wide = stock-picking market,
    narrow = factor-driven market."""
    vals = []
    for t in tickers:
        if t in close.columns:
            v = pct_return(close[t], sessions)
            if v is not None:
                vals.append(v)
    if len(vals) < 4:
        return {"n": len(vals)}
    s = pd.Series(vals)
    return {
        "n": len(vals), "sessions": sessions, "std_pp": round(float(s.std()), 2),
        "p25": round(float(s.quantile(.25)), 2), "median": round(float(s.median()), 2),
        "p75": round(float(s.quantile(.75)), 2), "pct_positive": round(float((s > 0).mean() * 100), 0),
    }


def drawdown_from_high(series: pd.Series) -> dict[str, float | None]:
    s = series.dropna()
    if s.empty:
        return {"from_52w_high_pct": None, "above_200d_pct": None}
    out = {"from_52w_high_pct": round((float(s.iloc[-1]) / float(s.max()) - 1) * 100, 2)}
    out["above_200d_pct"] = round((float(s.iloc[-1]) / float(s.tail(200).mean()) - 1) * 100, 2) if len(s) >= 150 else None
    return out


# ---- macro / credit -------------------------------------------------------

def macro_stats(series: pd.Series, unit: str = "pct") -> dict[str, Any]:
    """Level, changes and 1y context. Yield/spread changes are in basis points."""
    s = series.dropna()
    if s.empty:
        return {"last": None}
    last_date = s.index[-1]

    def at(days: int) -> float | None:
        prior = s[s.index <= last_date - pd.Timedelta(days=days)]
        return float(prior.iloc[-1]) if len(prior) else None

    scale = 100.0 if unit == "pct" else 1.0
    changes: dict[str, float | None] = {}
    if unit == "pct_level":  # quarterly: compare by observation, not calendar days
        for label, n in (("1Q", 1), ("2Q", 2), ("1Y", 4)):
            changes[label] = round(float(s.iloc[-1] - s.iloc[-1 - n]), 2) if len(s) > n else None
    horizons_days = () if unit == "pct_level" else (("1W", 7), ("1M", 30), ("3M", 91), ("6M", 182), ("1Y", 365))
    for label, days in horizons_days:
        base = at(days)
        if base is None:
            changes[label] = None
        elif unit == "pct" or unit == "pct_level":
            changes[label] = round((float(s.iloc[-1]) - base) * scale, 1)
        else:
            changes[label] = round((float(s.iloc[-1]) / base - 1) * 100, 2) if base else None
    year = s[s.index > last_date - pd.Timedelta(days=365)]
    pct_rank = round(float((year <= s.iloc[-1]).mean() * 100), 0) if len(year) else None
    return {
        "last": round(float(s.iloc[-1]), 3), "as_of": str(last_date.date()),
        "change": changes, "change_unit": "bp" if unit == "pct" else ("pp" if unit == "pct_level" else "%"),
        "range_1y": [round(float(year.min()), 3), round(float(year.max()), 3)] if len(year) else None,
        "percentile_1y": pct_rank,
    }


def downsample(series: pd.Series, max_points: int = 400, ndigits: int = 3) -> list[list[Any]]:
    s = series.dropna()
    if len(s) > max_points:
        step = math.ceil(len(s) / max_points)
        keep = s.iloc[::step]
        if keep.index[-1] != s.index[-1]:
            keep = pd.concat([keep, s.iloc[[-1]]])
        s = keep
    return [[str(i.date()), round(float(v), ndigits)] for i, v in s.items()]


def spread_implied_loss(oas_pct: float | None, recovery: float = ASSUMED_RECOVERY) -> float | None:
    """Crude annual default-intensity proxy: OAS / (1 - recovery). It conflates
    default compensation with liquidity and risk premia, so it is an upper-bound
    style gauge, not a measured default rate."""
    if oas_pct is None:
        return None
    return round(oas_pct / (1 - recovery), 2)


def rolling_corr_with(series_a: pd.Series, series_b: pd.Series, window: int = 60) -> dict[str, float | None]:
    """Correlation of daily changes (a) with daily changes (b), latest window."""
    joined = pd.concat([series_a.diff(), series_b], axis=1, keys=["a", "b"]).dropna().tail(window)
    if len(joined) < 20:
        return {"corr": None, "n": len(joined)}
    return {"corr": round(float(joined["a"].corr(joined["b"])), 2), "n": len(joined)}


# ---- news windows -----------------------------------------------------------

def window_for(age_days: float) -> str:
    return "week" if age_days <= 7 else ("month" if age_days <= 30 else "quarter")
