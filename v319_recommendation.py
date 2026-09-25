"""Ver319 recommendation gate — recovery-oriented, generation-aware.

Background (from analysis of Ver319 buy-ticket path):
  - Prediction simulation itself is stable; do not touch it here.
  - Final ◎ / 推奨 gate still uses Ver314-era fixed thresholds
    (EV 1.0–2.0, tickets <= 10, exclude "100%" labels).
  - That creates a generation mismatch: engine moved on, gate stayed at Ver314.

This module:
  1. Keeps the *same decision logic* as Ver314 so live behaviour does not jump.
  2. Tags every decision with gate_generation / threshold_source so DB analysis
     can separate "old gate" vs future recalibrated gates.
  3. Returns structured reason fields (reasons_list, diagnostics) for audit.
  4. Makes thresholds overridable without editing call sites.

Integrate by calling `v319_live_recommendation(result)` wherever
`_v305_live_recommendation` / `v314_live_recommendation` is used,
or apply `apply_v319_patch_to_module(app_module)`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


# ---------------------------------------------------------------------------
# Thresholds — still Ver314-era defaults.  Re-calibrate against current
# Ver319 simulation distribution + DB36 before changing these numbers.
# ---------------------------------------------------------------------------
GATE_GENERATION = "Ver319"
THRESHOLD_SOURCE = "Ver314_backtest_2026-07/08"  # provenance tag

V319_EV_MIN = 1.0
V319_EV_MAX = 2.0
V319_MAX_TICKETS = 10
V319_EXCLUDE_LABEL_SUBSTR: Sequence[str] = ("100%",)

# Optional extra diagnostics keys that callers may already put on result
_EXTRA_DIAG_KEYS = (
    "cover",
    "カバー",
    "参考回収率",
    "ref_return_rate",
    "model_return_rate",
    "starter_count",
    "出走数",
)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        x = float(v)
        if x != x:  # NaN
            return default
        return x
    except (TypeError, ValueError):
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except (TypeError, ValueError):
        return default


def _max_ev_from_result(result: dict) -> float:
    """Prefer precomputed max EV; otherwise derive from tickets."""
    for key in ("max_ev", "新判定最大EV", "maximum_ev", "ev_max"):
        if key in result and result.get(key) is not None:
            return _safe_float(result.get(key))
    tickets = result.get("tickets") or result.get("plan") or []
    evs: List[float] = []
    for t in tickets:
        if not isinstance(t, dict):
            continue
        if t.get("standalone_ev") is not None:
            evs.append(_safe_float(t.get("standalone_ev")))
            continue
        # Prefer EV-calibrated probability when present (Ver319 path)
        p = _safe_float(t.get("ev_probability", t.get("probability", 0.0)))
        o = _safe_float(t.get("odds", 0.0))
        if p > 0 and o > 0:
            # probability is expected in percent (0–100) in this codebase
            evs.append((p / 100.0) * o)
    return max(evs) if evs else 0.0


def _ticket_count_from_result(result: dict) -> int:
    for key in ("ticket_count", "点数", "n_tickets", "plan_size"):
        if key in result and result.get(key) is not None:
            return _safe_int(result.get(key))
    tickets = result.get("tickets") or result.get("plan") or []
    if isinstance(tickets, list):
        return len(tickets)
    return 0


def _meta_label_from_result(result: dict) -> str:
    for key in ("元判定", "meta_label", "judgment_label", "base_label"):
        if result.get(key):
            return str(result.get(key))
    return ""


def _collect_extra_diagnostics(result: dict) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in _EXTRA_DIAG_KEYS:
        if key in result and result.get(key) is not None:
            out[key] = result.get(key)
    return out


def v319_should_recommend(
    max_ev: float,
    ticket_count: int,
    meta_label: str = "",
    *,
    ev_min: float = V319_EV_MIN,
    ev_max: float = V319_EV_MAX,
    max_tickets: int = V319_MAX_TICKETS,
    exclude_substrings: Sequence[str] = V319_EXCLUDE_LABEL_SUBSTR,
) -> bool:
    """Return True if this race should be marked 推奨 under current gate rules."""
    label = str(meta_label or "")
    for bad in exclude_substrings:
        if bad in label:
            return False
    if ticket_count is None or int(ticket_count) > int(max_tickets):
        return False
    if ticket_count <= 0:
        return False
    ev = _safe_float(max_ev)
    return float(ev_min) <= ev <= float(ev_max)


def v319_live_recommendation(
    result: dict,
    *,
    ev_min: float = V319_EV_MIN,
    ev_max: float = V319_EV_MAX,
    max_tickets: int = V319_MAX_TICKETS,
    exclude_substrings: Sequence[str] = V319_EXCLUDE_LABEL_SUBSTR,
) -> dict:
    """Ver319 live recommendation decision (behaviour-compatible with Ver314).

    Returns a dict compatible with existing UI fields, plus audit fields:
      recommended, 推奨区分, 新推奨, label, score, 推奨スコア, reason,
      max_ev, ticket_count, rule, version,
      gate_generation, threshold_source, reasons_list, diagnostics
    """
    result = result or {}
    max_ev = _max_ev_from_result(result)
    ticket_count = _ticket_count_from_result(result)
    meta_label = _meta_label_from_result(result)
    diagnostics = _collect_extra_diagnostics(result)

    ok = v319_should_recommend(
        max_ev,
        ticket_count,
        meta_label,
        ev_min=ev_min,
        ev_max=ev_max,
        max_tickets=max_tickets,
        exclude_substrings=exclude_substrings,
    )

    common = {
        "max_ev": max_ev,
        "ticket_count": ticket_count,
        "rule": "v319_ev_band",
        "version": GATE_GENERATION,
        "gate_generation": GATE_GENERATION,
        "threshold_source": THRESHOLD_SOURCE,
        "thresholds": {
            "ev_min": float(ev_min),
            "ev_max": float(ev_max),
            "max_tickets": int(max_tickets),
            "exclude_label_substrings": list(exclude_substrings),
        },
        "diagnostics": diagnostics,
    }

    if ok:
        reason = (
            f"{GATE_GENERATION}: EV帯[{ev_min},{ev_max}] & 点数<={max_tickets}"
            f" (thresholds from {THRESHOLD_SOURCE})"
        )
        return {
            **common,
            "recommended": True,
            "推奨区分": "推奨",
            "新推奨": "◎強推奨",
            "label": "◎強推奨",
            "score": 10,
            "推奨スコア": 10,
            "reason": reason,
            "reasons_list": ["ev_in_band", "ticket_count_ok", "label_ok"],
        }

    reasons_list: List[str] = []
    reasons_human: List[str] = []
    if any(b in meta_label for b in exclude_substrings):
        reasons_list.append("excluded_label")
        reasons_human.append("100%候補除外")
    if ticket_count > max_tickets:
        reasons_list.append("too_many_tickets")
        reasons_human.append(f"点数{ticket_count}>{max_tickets}")
    if not (ev_min <= max_ev <= ev_max):
        reasons_list.append("ev_out_of_band")
        reasons_human.append(f"EV={max_ev:.3f}が帯外")
    if ticket_count <= 0:
        reasons_list.append("no_tickets")
        reasons_human.append("買い目なし")

    return {
        **common,
        "recommended": False,
        "推奨区分": "推奨外",
        "新推奨": "見送り",
        "label": "見送り",
        "score": 0,
        "推奨スコア": 0,
        "reason": f"{GATE_GENERATION}見送り: " + (", ".join(reasons_human) if reasons_human else "条件未達"),
        "reasons_list": reasons_list or ["conditions_not_met"],
    }


# ---------------------------------------------------------------------------
# Compatibility aliases so existing call sites keep working
# ---------------------------------------------------------------------------
v314_live_recommendation = v319_live_recommendation
v314_should_recommend = v319_should_recommend


def apply_v319_over_v305(v305_fn):
    """Wrapper: use Ver319 gate instead of Ver305/Ver314."""

    def wrapped(result: dict) -> dict:
        return v319_live_recommendation(result)

    wrapped.__name__ = getattr(v305_fn, "__name__", "v319_wrapped")
    wrapped.__doc__ = (
        "Ver319 recovery-oriented recommendation "
        "(same thresholds as Ver314, with generation tags)."
    )
    return wrapped


def apply_v319_patch_to_module(mod) -> Dict[str, Any]:
    """Monkey-patch an already-imported app module."""
    changed: Dict[str, Any] = {}
    for attr in (
        "_v305_live_recommendation",
        "_v301_live_recommendation",
        "_v314_live_recommendation",
    ):
        if hasattr(mod, attr):
            setattr(mod, attr, v319_live_recommendation)
            changed[attr] = "v319_live_recommendation"

    for name in ("APP_VERSION", "APP_VERSION_LABEL", "CURRENT_VERSION", "VERSION"):
        if hasattr(mod, name):
            # Do not force version string change; leave that to the app.
            # Only record that the gate is now Ver319-aware.
            pass
    changed["gate"] = GATE_GENERATION
    changed["threshold_source"] = THRESHOLD_SOURCE
    return changed


if __name__ == "__main__":
    samples = [
        {"max_ev": 1.89, "ticket_count": 11, "元判定": "非推奨"},
        {"max_ev": 1.89, "ticket_count": 10, "元判定": "非推奨"},
        {"max_ev": 7.4, "ticket_count": 8, "元判定": "回収率基準を満たす"},
        {"max_ev": 1.5, "ticket_count": 8, "元判定": "回収率100%候補・検証中"},
        {"max_ev": 1.03, "ticket_count": 8, "元判定": "非推奨", "カバー": 40, "参考回収率": 38},
    ]
    for s in samples:
        r = v319_live_recommendation(s)
        print(
            s.get("max_ev"),
            s.get("ticket_count"),
            "->",
            r["新推奨"],
            "|",
            r["reason"],
            "|",
            r["reasons_list"],
            "|",
            r.get("diagnostics"),
        )
