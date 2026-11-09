from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import lib_semi_analytics as sa  # noqa: E402
from synthesize_semiconductor import validate_synthesis  # noqa: E402


def _close(n=300):
    idx = pd.bdate_range("2025-01-01", periods=n)
    rng = np.random.default_rng(1)
    base = rng.normal(0, 0.01, n)
    cols = {t: 100 * np.cumprod(1 + base + rng.normal(0, 0.005, n)) for t in ("NVDA", "AMD", "ASML", "AMAT", "LRCX")}
    return pd.DataFrame(cols, index=idx)


def test_segment_index_is_equal_weight_and_skips_thin_segments():
    close = _close()
    idx, members = sa.segment_indexes(close)
    assert set(idx.columns) == {"ai_compute", "equipment"}  # others have <2 usable members
    manual = (1 + close[["ASML", "AMAT", "LRCX"]].pct_change().mean(axis=1).fillna(0)).cumprod() * 100
    assert abs(idx["equipment"].iloc[-1] - manual.iloc[-1]) < 1e-6


def test_correlation_and_shift_are_bounded():
    idx, _ = sa.segment_indexes(_close())
    r = idx.pct_change()
    m = sa.correlation_matrix(r, 60)
    assert ((m.stack() <= 1.0001) & (m.stack() >= -1.0001)).all()
    assert all(abs(s["change"]) <= 2 for s in sa.correlation_shifts(r, 60))


def test_macro_stats_units_and_quarterly_labels():
    idx = pd.date_range("2025-01-01", periods=400)
    y = pd.Series(np.linspace(4.0, 5.0, 400), index=idx)
    st = sa.macro_stats(y, "pct")
    assert st["change_unit"] == "bp" and st["change"]["1M"] > 0
    q = pd.Series([1.0, 1.1, 1.3, 1.2, 1.5], index=pd.date_range("2025-01-01", periods=5, freq="QS"))
    qs = sa.macro_stats(q, "pct_level")
    assert qs["change"] == {"1Q": 0.3, "2Q": 0.2, "1Y": 0.5}


def test_spread_implied_loss():
    assert sa.spread_implied_loss(3.0) == 5.0 and sa.spread_implied_loss(None) is None


def test_validator_strips_unknown_ids_and_flags_unfalsifiable_themes():
    obj = {"bottom_line": "x", "rates_credit": {"read": "r"},
           "horizons": {k: {"synthesis": "s", "source_ids": ["Hok", "Hbad"]} for k in ("week", "month", "quarter")},
           "themes": [{"title": "t", "confidence": "sure", "evidence": [{"claim": "c", "source_ids": ["Hbad"]}]}]}
    cleaned, problems = validate_synthesis(obj, {"Hok"})
    assert cleaned["horizons"]["week"]["source_ids"] == ["Hok"]
    text = " ".join(problems)
    assert "unknown source ids" in text and "no falsifiers" in text and "no counter-evidence" in text
    assert "fewer than two evidence" in text and "confidence must be" in text
