"""Ver314 recommendation gate — recovery-rate oriented.

Past-data simulation (Ver305/Ver313 export CSVs):
  - Current ◎-only (Ver313): ~55.6% recommended ROI
  - EV band 1.0–2.0 & ticket_count <= 10: ~100–111% recommended ROI
  - High EV / "100%候補" labels were anti-predictive of actual ROI

Integrate by calling `v314_live_recommendation(result)` wherever
`_v305_live_recommendation` / live recommendation is used, or apply
`apply_v314_patch_to_module(app_module)`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


# Tunable thresholds (backtested on 2026-07/08 exports)
V314_EV_MIN = 1.0
V314_EV_MAX = 2.0
V314_MAX_TICKETS = 10
V314_EXCLUDE_LABEL_SUBSTR = ("100%",)


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
    evs = []
    for t in tickets:
        if not isinstance(t, dict):
            continue
        if t.get("standalone_ev") is not None:
            evs.append(_safe_float(t.get("standalone_ev")))
            continue
        p = _safe_float(t.get("ev_probability", t.get("probability", 0.0)))
        o = _safe_float(t.get("odds", 0.0))
        if p > 0 and o > 0:
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


def v314_should_recommend(
    max_ev: float,
    ticket_count: int,
    meta_label: str = "",
    *,
    ev_min: float = V314_EV_MIN,
    ev_max: float = V314_EV_MAX,
    max_tickets: int = V314_MAX_TICKETS,
) -> bool:
    """Return True if this race should be marked 推奨 under Ver314 rules."""
    label = str(meta_label or "")
    for bad in V314_EXCLUDE_LABEL_SUBSTR:
        if bad in label:
            return False
    if ticket_count is None or int(ticket_count) > int(max_tickets):
        return False
    if ticket_count <= 0:
        return False
    ev = _safe_float(max_ev)
    return float(ev_min) <= ev <= float(ev_max)


def v314_live_recommendation(result: dict) -> dict:
    """Ver314 live recommendation decision.

    Returns a dict compatible with existing UI fields:
      recommended: bool
      label: str  (◎強推奨 / 見送り / etc.)
      score: int  (10 = strong recommend, 0 = skip)
      reason: str
      max_ev, ticket_count, rule
    """
    result = result or {}
    max_ev = _max_ev_from_result(result)
    ticket_count = _ticket_count_from_result(result)
    meta_label = _meta_label_from_result(result)

    ok = v314_should_recommend(max_ev, ticket_count, meta_label)

    if ok:
        return {
            "recommended": True,
            "推奨区分": "推奨",
            "新推奨": "◎強推奨",
            "label": "◎強推奨",
            "score": 10,
            "推奨スコア": 10,
            "reason": f"Ver314: EV帯[{V314_EV_MIN},{V314_EV_MAX}] & 点数<={V314_MAX_TICKETS}",
            "max_ev": max_ev,
            "ticket_count": ticket_count,
            "rule": "v314_ev_band",
            "version": "Ver314",
        }

    reasons = []
    if any(b in meta_label for b in V314_EXCLUDE_LABEL_SUBSTR):
        reasons.append("100%候補除外")
    if ticket_count > V314_MAX_TICKETS:
        reasons.append(f"点数{ticket_count}>{V314_MAX_TICKETS}")
    if not (V314_EV_MIN <= max_ev <= V314_EV_MAX):
        reasons.append(f"EV={max_ev:.3f}が帯外")
    if ticket_count <= 0:
        reasons.append("買い目なし")

    return {
        "recommended": False,
        "推奨区分": "推奨外",
        "新推奨": "見送り",
        "label": "見送り",
        "score": 0,
        "推奨スコア": 0,
        "reason": "Ver314見送り: " + (", ".join(reasons) if reasons else "条件未達"),
        "max_ev": max_ev,
        "ticket_count": ticket_count,
        "rule": "v314_ev_band",
        "version": "Ver314",
    }


def apply_v314_over_v305(v305_fn):
    """Wrapper: use Ver314 gate instead of Ver305."""

    def wrapped(result: dict) -> dict:
        return v314_live_recommendation(result)

    wrapped.__name__ = getattr(v305_fn, "__name__", "v314_wrapped")
    wrapped.__doc__ = "Ver314 recovery-oriented recommendation (replaces Ver305 gate)."
    return wrapped


def apply_v314_patch_to_module(mod) -> Dict[str, Any]:
    """Monkey-patch an already-imported app module."""
    changed = {}
    if hasattr(mod, "_v305_live_recommendation"):
        mod._v305_live_recommendation = v314_live_recommendation
        changed["_v305_live_recommendation"] = "v314_live_recommendation"
    if hasattr(mod, "_v301_live_recommendation"):
        mod._v301_live_recommendation = v314_live_recommendation
        changed["_v301_live_recommendation"] = "v314_live_recommendation"

    for name in ("APP_VERSION", "APP_VERSION_LABEL", "CURRENT_VERSION", "VERSION"):
        if hasattr(mod, name):
            setattr(mod, name, "Ver314")
            changed[name] = "Ver314"
    return changed


if __name__ == "__main__":
    samples = [
        {"max_ev": 1.89, "ticket_count": 11, "元判定": "非推奨"},
        {"max_ev": 1.89, "ticket_count": 10, "元判定": "非推奨"},
        {"max_ev": 7.4, "ticket_count": 8, "元判定": "回収率基準を満たす"},
        {"max_ev": 1.5, "ticket_count": 8, "元判定": "回収率100%候補・検証中"},
        {"max_ev": 1.03, "ticket_count": 8, "元判定": "非推奨"},
    ]
    for s in samples:
        r = v314_live_recommendation(s)
        print(s, "->", r["新推奨"], r["reason"])
