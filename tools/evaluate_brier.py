#!/usr/bin/env python3
"""Lap1 pairwise Brier evaluation for the prospective store (beta-prime policy v1.0.2, method L).

Definition (Lap1 pairwise Brier, fixed spec):
  pairs a<b by car number; y_ab = 1 if a is ahead of b in the actual Lap1 order
  p_ab = sum_o c_o * 1{pos_o(a) < pos_o(b)} / T,  T = sum_o c_o (lap_order_counts["1"])
  BS_r = mean over pairs of (p_ab - y_ab)^2 ; BS_macro = mean over races of BS_r
  No smoothing, rounding or clipping. 0.25 = uninformative (p = 0.5 for every pair).

Exclusions (design v0.3 §3.5, policy §5), applied before any Brier is computed:
  error           record status != ok/late, or no lap_order_counts
  late            status='late', sim_finished_at >= deadline, or commit time >= deadline
  start_change    deadline/start changed between record and result and the commit is not before the new deadline
  trials          T != sim_trials or sim_trials != requested_trials or validation_full_trials != 1
  lap1_missing    no result row with lap1_order_json (latest fetched_at among valid rows is used)
  tie             duplicate car in the actual Lap1 order
  car_set         predicted car set != result car set != Lap1 car set, or count keys with another car set
  divergence      kessha_cars non-empty or retrial_text true (decision 8: recorded, excluded from the main tally)
  provenance      target_result_absent != 1 (the DB already had the race's result)

Usage:
  python tools/evaluate_brier.py --store prospective_collect.sqlite3 --config ver326_base [--from 2026-11-14] [--to ...]
         [--include-divergence] [--bootstrap 10000] [--json out.json] [--md out.md]
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import random
import sqlite3
import statistics
import sys
from collections import Counter

JST = dt.timezone(dt.timedelta(hours=9))
ORDER = ["error", "provenance", "late", "start_change", "trials", "lap1_missing", "tie", "car_set", "divergence"]
LABEL_JA = {
    "error": "シミュレーションまたは診断エラー",
    "provenance": "入力の由来が未確認（DB に対象レースの結果あり）",
    "late": "締切後の記録完了",
    "start_change": "発走時刻の変更で判定不能",
    "trials": "T != sim_trials",
    "lap1_missing": "実際の Lap1 順位の欠損",
    "tie": "同着",
    "car_set": "車集合不一致",
    "divergence": "本番経路との差（欠車・再試走、決定事項 8）",
}


def parse_iso(s):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s))
        return d if d.tzinfo else d.replace(tzinfo=JST)
    except ValueError:
        return None


def hhmm_on(race_key: str, hhmm: str | None):
    """'19:38' or '24:30' on the race date (JST)."""
    if not hhmm or ":" not in str(hhmm):
        return None
    try:
        h, m = (int(x) for x in str(hhmm).split(":")[:2])
        base = dt.datetime.strptime(race_key[:8], "%Y%m%d").replace(tzinfo=JST)
        return base + dt.timedelta(hours=h, minutes=m)
    except ValueError:
        return None


def jload(s, default=None):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


def pair_probs(counts: dict) -> tuple[dict, int, set]:
    """counts: {"1-2-3-...": c}. Returns ({(a,b): p_ab}, T, car_set)."""
    T = 0
    ahead = Counter()
    cars = None
    for key, c in counts.items():
        order = [int(x) for x in str(key).split("-")]
        if len(order) != len(set(order)):
            raise ValueError("duplicate cars in count key")
        s = set(order)
        if cars is None:
            cars = s
        elif s != cars:
            raise ValueError("count keys with different car sets")
        c = int(c)
        T += c
        pos = {car: i for i, car in enumerate(order)}
        for a, b in itertools.combinations(sorted(order), 2):
            if pos[a] < pos[b]:
                ahead[(a, b)] += c
    cars = set(cars or [])
    probs = {(a, b): ahead[(a, b)] / T for a, b in itertools.combinations(sorted(cars), 2)} if T else {}
    return probs, T, cars


def race_brier(probs: dict, lap1: list[int]) -> tuple[float, float, int]:
    pos = {car: i for i, car in enumerate(lap1)}
    se = []
    hit = []
    for (a, b), p in probs.items():
        y = 1.0 if pos[a] < pos[b] else 0.0
        se.append((p - y) ** 2)
        if p != 0.5:
            hit.append(1.0 if (p > 0.5) == (y == 1.0) else 0.0)
    return sum(se) / len(se), (sum(hit) / len(hit) if hit else float("nan")), len(se)


def load(con, config_id, d_from, d_to):
    rows = con.execute(
        "SELECT l.*, c.committed_at FROM v332_prospective_alpha_log l LEFT JOIN v332_commit_log c ON c.alpha_log_id = l.id "
        "WHERE l.config_id = ? ORDER BY l.id", (config_id,)).fetchall()
    cols = [d[0] for d in con.execute("SELECT l.*, c.committed_at FROM v332_prospective_alpha_log l "
                                       "LEFT JOIN v332_commit_log c ON c.alpha_log_id = l.id LIMIT 0").description]
    recs = [dict(zip(cols, r)) for r in rows]
    if d_from:
        recs = [r for r in recs if r["race_key"][:8] >= d_from.replace("-", "")]
    if d_to:
        recs = [r for r in recs if r["race_key"][:8] <= d_to.replace("-", "")]
    # §3.4: one row per race_key/config; actions first, then app
    best = {}
    for r in recs:
        k = r["race_key"]
        if k not in best or (best[k]["source"] != "actions" and r["source"] == "actions"):
            best[k] = r
    res = {}
    for k in best:
        row = con.execute(
            "SELECT race_key, fetched_at, lap1_order_json, car_set_json, change_start_flg, telvote_time_at_result, "
            "race_start_time_at_result FROM v332_prospective_results WHERE race_key = ? AND lap1_order_json IS NOT NULL "
            "ORDER BY fetched_at DESC LIMIT 1", (k,)).fetchone()
        if row:
            res[k] = dict(zip(["race_key", "fetched_at", "lap1", "car_set", "change_start_flg", "telvote", "start"], row))
    return [best[k] for k in sorted(best)], res


def judge(r, res):
    """Return (reason or None, details dict)."""
    det = {}
    if r["status"] not in ("ok", "late") or not r.get("lap_order_counts_json"):
        return "error", {"status": r["status"], "error": r.get("error_text")}
    if int(r.get("target_result_absent") or 0) != 1:
        return "provenance", {}
    dl = parse_iso(r["telvote_time"])
    fin = parse_iso(r["sim_finished_at"])
    com = parse_iso(r.get("committed_at"))
    if r["status"] == "late" or dl is None or fin is None or com is None or not (fin < dl and com < dl):
        return "late", {"deadline": r["telvote_time"], "sim_finished_at": r["sim_finished_at"], "committed_at": r.get("committed_at")}
    rr = res.get(r["race_key"])
    if rr:
        new_dl = hhmm_on(r["race_key"], rr["telvote"])
        changed = (str(rr.get("change_start_flg") or 0) != str(r.get("change_start_flg") or 0)) or (
            new_dl is not None and new_dl != dl)
        if changed and (new_dl is None or not com < new_dl):
            return "start_change", {"deadline_at_record": r["telvote_time"], "deadline_at_result": rr["telvote"]}
        det["deadline_changed"] = bool(changed)
    counts = (jload(r["lap_order_counts_json"], {}) or {}).get("1") or {}
    try:
        probs, T, cars = pair_probs(counts)
    except ValueError as e:
        return "car_set", {"error": str(e)}
    if not (T == int(r["sim_trials"] or -1) == int(r["requested_trials"] or -2)) or int(r.get("validation_full_trials") or 0) != 1:
        return "trials", {"T": T, "sim_trials": r["sim_trials"], "requested": r["requested_trials"]}
    if not rr:
        return "lap1_missing", {}
    lap1 = jload(rr["lap1"], None)
    if not isinstance(lap1, list) or not lap1:
        return "lap1_missing", {"lap1_invalid": True}
    if len(lap1) != len(set(lap1)):
        return "tie", {"lap1": lap1}
    pred_set = set(jload(r["car_set_json"], []) or [])
    res_set = set(jload(rr["car_set"], []) or [])
    if not (pred_set == cars == set(lap1) == res_set):
        return "car_set", {"pred": sorted(pred_set), "counts": sorted(cars), "lap1": sorted(lap1), "result": sorted(res_set)}
    div = jload(r.get("divergence_flags_json"), {}) or {}
    det.update({"probs": probs, "lap1": lap1, "T": T,
                "divergence": bool(div.get("kessha_cars")) or bool(div.get("retrial_text")),
                "kessha_cars": div.get("kessha_cars") or [], "retrial_text": bool(div.get("retrial_text"))})
    if det["divergence"]:
        return "divergence", det
    return None, det


def bootstrap_ci(xs, n, seed):
    if len(xs) < 2 or n <= 0:
        return None
    rnd = random.Random(seed)
    m = len(xs)
    means = sorted(sum(xs[rnd.randrange(m)] for _ in range(m)) / m for _ in range(n))
    return [means[int(0.025 * (n - 1))], means[int(0.975 * (n - 1))]]


def evaluate(store, config_id, d_from=None, d_to=None, include_divergence=False, n_boot=10000, seed=20260719):
    con = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    recs, res = load(con, config_id, d_from, d_to)
    races, excluded = [], []
    for r in recs:
        reason, det = judge(r, res)
        if reason == "divergence" and include_divergence:
            reason = None
        if reason:
            excluded.append({"race_key": r["race_key"], "reason": reason, "label": LABEL_JA[reason],
                             "detail": {k: v for k, v in det.items() if k not in ("probs",)}})
            continue
        bs, acc, npairs = race_brier(det["probs"], det["lap1"])
        races.append({"race_key": r["race_key"], "venue": r["race_key"].split("_")[1], "date": r["race_key"][:8],
                      "n_cars": len(det["lap1"]), "pairs": npairs, "bs": bs, "delta_vs_uninformative": bs - 0.25,
                      "pair_accuracy": acc, "deadline_changed": det.get("deadline_changed", False),
                      "divergence": det.get("divergence", False)})
    bss = [x["bs"] for x in races]
    pair_accs = [
        x["pair_accuracy"] for x in races
        if x["pair_accuracy"] == x["pair_accuracy"]
    ]
    out = {
        "generated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "store": store, "config_id": config_id, "from": d_from, "to": d_to,
        "include_divergence": include_divergence,
        "candidates": len(recs), "evaluated": len(races),
        "bs_macro": (sum(bss) / len(bss)) if bss else None,
        "bs_median": statistics.median(bss) if bss else None,
        "uninformative": 0.25,
        "delta_vs_uninformative_mean": (sum(bss) / len(bss) - 0.25) if bss else None,
        "races_better_than_uninformative": sum(1 for b in bss if b < 0.25),
        "bootstrap_ci95_bs_macro": bootstrap_ci(bss, n_boot, seed),
        "bootstrap_note": "race-level resampling; descriptive only (the test method is fixed separately before the main measurement)",
        "pair_accuracy_mean": statistics.fmean(pair_accs) if pair_accs else None,
        "excluded_by_reason": {k: sum(1 for e in excluded if e["reason"] == k) for k in ORDER},
        "excluded": excluded,
        "distribution": {"by_date": dict(Counter(x["date"] for x in races)),
                         "by_venue": dict(Counter(x["venue"] for x in races)),
                         "excluded_by_date": dict(Counter(e["race_key"][:8] for e in excluded)),
                         "excluded_by_venue": dict(Counter(e["race_key"].split("_")[1] for e in excluded))},
        "races": races,
    }
    return out


def to_md(o):
    f = lambda v: "—" if v is None else f"{v:.4f}"
    L = [f"# Lap1 pairwise Brier（{o['config_id']}）", "",
         f"- 作成：{o['generated_at']}／記録ファイル：`{o['store']}`／期間：{o['from'] or '—'}〜{o['to'] or '—'}",
         f"- 候補 {o['candidates']} R、評価 {o['evaluated']} R、除外 {len(o['excluded'])} R"
         + ("（欠車・再試走を含めた集計）" if o["include_divergence"] else ""),
         f"- BS_macro = **{f(o['bs_macro'])}**（無情報 0.25 との差 {f(o['delta_vs_uninformative_mean'])}、"
         f"0.25 を下回った R：{o['races_better_than_uninformative']}/{o['evaluated']}）",
         f"- 中央値 {f(o['bs_median'])}、ペア一致率の平均 {f(o['pair_accuracy_mean'])}",
         f"- 95% ブートストラップ区間（レース単位、記述用）：{o['bootstrap_ci95_bs_macro']}", "",
         "## 除外", "", "| 理由 | 件数 |", "|---|---|"]
    for k in ORDER:
        L.append(f"| {LABEL_JA[k]} | {o['excluded_by_reason'][k]} |")
    if o["excluded"]:
        L += ["", "| race_key | 理由 |", "|---|---|"] + [f"| {e['race_key']} | {e['label']} |" for e in o["excluded"]]
    L += ["", "## レース別", "", "| race_key | 車数 | BS_r | 0.25 との差 | ペア一致率 |", "|---|---|---|---|---|"]
    for x in o["races"]:
        L.append(f"| {x['race_key']} | {x['n_cars']} | {x['bs']:.4f} | {x['delta_vs_uninformative']:+.4f} | {x['pair_accuracy']:.3f} |")
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--from", dest="d_from")
    ap.add_argument("--to", dest="d_to")
    ap.add_argument("--include-divergence", action="store_true")
    ap.add_argument("--bootstrap", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=20260719)
    ap.add_argument("--json")
    ap.add_argument("--md")
    a = ap.parse_args(argv)
    o = evaluate(a.store, a.config, a.d_from, a.d_to, a.include_divergence, a.bootstrap, a.seed)
    if a.json:
        open(a.json, "w", encoding="utf-8").write(json.dumps(o, ensure_ascii=False, indent=1))
    md = to_md(o)
    if a.md:
        open(a.md, "w", encoding="utf-8").write(md)
    sys.stdout.write(md)


if __name__ == "__main__":
    main()
