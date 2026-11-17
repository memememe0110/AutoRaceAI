#!/usr/bin/env python3
"""Read-only historical Lap1 pairwise baseline and frozen 29R handicap audit."""
from __future__ import annotations
import argparse
import csv
import itertools
import json
import math
import random
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from pathlib import Path

MIN_N = 10
PAIR_RE = re.compile(r'^([1-8])[ \t]+[^\n]*\n[^\n]*\n[^\n]*?ハンデ\s*(\d+)\s*m', re.M)
PAIR_DEFINITION = (
    'For each unordered car pair a < b, predict P(car a finishes ahead of car b). '
    'The outcome is 1 iff Lap1 position(a) < Lap1 position(b); otherwise 0. '
    'Pairs are formed from the exact observed race car set.'
)


def cols(con: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in con.execute(f'PRAGMA table_info("{table}")')]


def choose(names: list[str], candidates: list[str]) -> str | None:
    return next((name for name in candidates if name in names), None)


def qid(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_handicaps(raw: str) -> dict[int, int]:
    return {int(car): int(handicap) for car, handicap in PAIR_RE.findall(raw or '')}


def schema_audit(db_path: str) -> dict:
    con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    con.execute('PRAGMA query_only=ON')
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    audit = {'read_only': True, 'tables_present': sorted(tables), 'required_tables': {}}
    for table in ('result_laps', 'result_entries'):
        if table not in tables:
            audit['required_tables'][table] = {'present': False, 'columns': []}
        else:
            audit['required_tables'][table] = {'present': True, 'columns': cols(con, table)}
    if all(audit['required_tables'][t]['present'] for t in ('result_laps', 'result_entries')):
        lc, ec = cols(con, 'result_laps'), cols(con, 'result_entries')
        selected = {
            'lap_race_key': choose(lc, ['race_key']), 'lap_car': choose(lc, ['car_no', 'car']),
            'lap_position': choose(lc, ['position', 'rank']), 'lap_no': choose(lc, ['lap_no']),
            'lap_label': choose(lc, ['lap_label']), 'entry_race_key': choose(ec, ['race_key']),
            'entry_car': choose(ec, ['car_no', 'car']),
            'entry_handicap': choose(ec, ['handicap', 'handicap_m', 'handicap_value']),
            'entry_status_candidates': [x for x in ('accident_name', 'accidentName', 'accident', 'status', 'entry_status') if x in ec],
        }
        audit['selected_columns'] = selected
        required = ('lap_race_key', 'lap_car', 'lap_position', 'entry_race_key', 'entry_car')
        audit['schema_ready'] = all(selected[x] for x in required) and bool(selected['lap_no'] or selected['lap_label'])
    else:
        audit['schema_ready'] = False
    con.close()
    return audit


def load_historical(db_path: str, audit: dict) -> tuple[list[dict], dict]:
    con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    con.execute('PRAGMA query_only=ON')
    s = audit['selected_columns']
    lr, lc, lp = s['lap_race_key'], s['lap_car'], s['lap_position']
    ln, ll = s['lap_no'], s['lap_label']
    er, ec = s['entry_race_key'], s['entry_car']
    eh = s['entry_handicap']
    date_expr = f'substr({qid(lr)},1,8)'
    # Mother population is every race represented in result_laps, not result_entries.
    population = {row[0] for row in con.execute(
        f"SELECT DISTINCT {qid(lr)} FROM result_laps WHERE {date_expr} < '20261114'")}
    entry_cols = [er, ec] + ([eh] if eh else [])
    status_cols = s['entry_status_candidates']
    # Status values are collected for audit only. No generic status text is interpreted as absence.
    entry_rows = con.execute(
        f"SELECT {','.join(qid(x) for x in entry_cols + status_cols)} FROM result_entries "
        f"WHERE substr({qid(er)},1,8) < '20261114' ORDER BY {qid(er)}").fetchall()
    entries = defaultdict(set)
    entry_counts = Counter()
    invalid_entry_car = set()
    handicaps = {}
    status_values = Counter()
    for row in entry_rows:
        race = row[0]
        car = as_int(row[1])
        if car is None or not 1 <= car <= 8:
            invalid_entry_car.add(race)
        else:
            entries[race].add(car)
            entry_counts[(race, car)] += 1
            if eh:
                raw_h = row[2]
                if raw_h is not None:
                    try:
                        handicaps[(race, car)] = float(raw_h)
                    except (TypeError, ValueError):
                        pass
        offset = 3 if eh else 2
        for index, column in enumerate(status_cols):
            value = row[offset + index]
            if value is not None and str(value).strip():
                status_values[(column, str(value).strip())] += 1

    def fetch_lap_rows(column: str, predicate: str):
        sql = (f"SELECT {qid(lr)}, {qid(lc)}, {qid(lp)} FROM result_laps "
               f"WHERE ({predicate}) AND {date_expr} < '20261114' ORDER BY {qid(lr)}")
        result = defaultdict(list)
        malformed = set()
        for race, car_raw, pos_raw in con.execute(sql):
            car, pos = as_int(car_raw), as_int(pos_raw)
            if car is None or pos is None or not 1 <= car <= 8 or pos < 1:
                malformed.add(race)
            else:
                result[race].append((pos, car))
        return result, malformed

    by_no, malformed_no = ({}, set())
    by_label, malformed_label = ({}, set())
    if ln:
        by_no, malformed_no = fetch_lap_rows(ln, f'{qid(ln)}=1')
    if ll:
        by_label, malformed_label = fetch_lap_rows(ll, f"{qid(ll)} IN ('1周目','1周')")
    con.close()

    valid = []
    primary = Counter()
    flag_counts = Counter()
    status_audit = [{'column': k[0], 'value': k[1], 'count': v} for k, v in status_values.items()]
    race_audit = []
    for race in sorted(population):
        flags = set()
        ent = entries.get(race, set())
        no_rows = by_no.get(race, [])
        label_rows = by_label.get(race, [])
        if race in invalid_entry_car:
            flags.add('entry_car_parse_invalid')
        if any(entry_counts[(race, car)] > 1 for car in ent):
            flags.add('duplicate_entry_car')
        if race in malformed_no or race in malformed_label:
            flags.add('lap1_value_parse_invalid')
        if no_rows and label_rows and sorted(no_rows) != sorted(label_rows):
            flags.add('lap_no_label_conflict')
        chosen_rows = no_rows if no_rows else label_rows
        if not chosen_rows:
            flags.add('lap1_missing')
        cars = [car for _, car in chosen_rows]
        positions = [pos for pos, _ in chosen_rows]
        if len(cars) != len(set(cars)):
            flags.add('duplicate_lap_car')
        if len(positions) != len(set(positions)):
            flags.add('duplicate_lap_position')
        if not ent:
            flags.add('entry_zero_rows')
        if ent and set(cars) != ent:
            flags.add('entry_lap_car_set_mismatch')

        # One mutually exclusive primary reason; additional observations remain in flags.
        if 'lap_no_label_conflict' in flags:
            reason = 'lap_no_label_conflict'
        elif 'lap1_value_parse_invalid' in flags:
            reason = 'lap1_value_parse_invalid'
        elif not chosen_rows:
            reason = 'lap1_missing'
        elif not ent:
            reason = 'entry_zero_rows'
        elif 'entry_car_parse_invalid' in flags:
            reason = 'entry_car_parse_invalid'
        elif 'duplicate_entry_car' in flags or 'duplicate_lap_car' in flags or 'duplicate_lap_position' in flags:
            reason = 'duplicate_rows'
        elif 'entry_lap_car_set_mismatch' in flags:
            reason = 'car_set_mismatch_unclassified'
        else:
            reason = 'included'
        primary[reason] += 1
        race_audit.append({'race_key': race, 'primary_reason': reason, 'flags': sorted(flags)})
        for flag in flags:
            flag_counts[flag] += 1
        if reason == 'included':
            ordered = [car for _, car in sorted(chosen_rows)]
            valid.append({
                'race_key': race, 'date': race[:8],
                'venue': race.split('_')[1] if '_' in race else 'unknown',
                'order': ordered,
                'handicaps': {car: handicaps[(race, car)] for car in ent if (race, car) in handicaps},
            })
    audit.update({
        'historical_mother_population_result_laps': len(population),
        'historical_entry_races': len(entries),
        'historical_races_with_lap_no_1': len(by_no),
        'historical_races_with_lap_label_1': len(by_label),
        'historical_valid_races': len(valid),
        'primary_reason_counts': dict(primary), 'flag_counts_nonexclusive': dict(flag_counts),
        'entry_status_value_audit_only': status_audit,
        'race_exclusion_audit': race_audit,
        'absent_accident_disqualification_classification': 'NOT_CLASSIFIED: raw-value audit only; no unverified text matching',
        'pair_definition': PAIR_DEFINITION,
    })
    return valid, audit


def periods():
    return {'1m': ('20261014', '20261113'), '3m': ('20260814', '20261113'),
            'all_history': (None, '20261113')}


def percentile(values: list[int], q: float):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * q
    lo, hi = math.floor(index), math.ceil(index)
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - index) + values[hi] * (index - lo)


def bootstrap_pair_ci(values: list[int], seed: int, n_boot: int = 1000):
    if len(values) < 2:
        return None, None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return means[int(.025 * (n_boot - 1))], means[int(.975 * (n_boot - 1))]


def calc_baseline(races: list[dict], label: str) -> dict:
    counts = defaultdict(lambda: [0, 0])
    for race in races:
        pos = {car: i for i, car in enumerate(race['order'])}
        for a, b in itertools.combinations(sorted(pos), 2):
            counts[(a, b)][1] += 1
            counts[(a, b)][0] += int(pos[a] < pos[b])
    probs = {pair: (v[0] / v[1] if v[1] >= MIN_N else .5) for pair, v in counts.items()}
    macro, sparse_macro = [], []
    micro_sum = micro_n = 0
    for race in races:
        pos = {car: i for i, car in enumerate(race['order'])}
        errors, dense_errors = [], []
        for a, b in itertools.combinations(sorted(pos), 2):
            n = counts[(a, b)][1]
            error = (probs[(a, b)] - float(pos[a] < pos[b])) ** 2
            errors.append(error)
            micro_sum += error
            micro_n += 1
            if n >= MIN_N:
                dense_errors.append(error)
        if errors:
            macro.append(statistics.mean(errors))
        if dense_errors:
            sparse_macro.append(statistics.mean(dense_errors))
    ns = [v[1] for v in counts.values()]
    return {
        'period': label, 'races': len(races),
        'pair_observations': sum(len(r['order']) * (len(r['order']) - 1) // 2 for r in races),
        'BS_baseline_with_fallback_macro': statistics.mean(macro) if macro else None,
        'BS_baseline_with_fallback_micro': micro_sum / micro_n if micro_n else None,
        'BS_baseline_excluding_sparse_macro': statistics.mean(sparse_macro) if sparse_macro else None,
        'sparse_excluding_eligible_races': len(sparse_macro),
        'sparse_excluding_pair_observations': sum(1 for race in races for a, b in itertools.combinations(sorted(race['order']), 2) if counts[(a, b)][1] >= MIN_N),
        'sparse_pair_count': sum(n < MIN_N for n in ns), 'unique_pair_count': len(counts),
        'pair_n_p10': percentile(ns, .10), 'pair_n_p50': percentile(ns, .50), 'pair_n_p90': percentile(ns, .90),
        'pairs': counts, 'probs': probs,
    }


def read_snapshot(path: str) -> dict:
    con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    con.execute('PRAGMA query_only=ON')
    required = {'v332_prospective_alpha_log', 'v332_prospective_results'}
    have = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= have:
        raise RuntimeError(f'snapshot tables missing: {sorted(required-have)}')
    result_cols = cols(con, 'v332_prospective_results')
    if 'fetched_at' not in result_cols:
        raise RuntimeError('snapshot schema missing fetched_at')
    rows = con.execute('''SELECT a.race_key,a.raw_text,a.car_set_json,a.lap_order_counts_json,
        r.lap1_order_json,r.finish_order_json,r.car_set_json
        FROM v332_prospective_alpha_log a
        LEFT JOIN v332_prospective_results r
          ON r.race_key=a.race_key AND r.fetched_at=(SELECT MAX(r2.fetched_at) FROM v332_prospective_results r2 WHERE r2.race_key=a.race_key)
        WHERE a.config_id='ver326_1_base' AND substr(a.race_key,1,8) BETWEEN '20261114' AND '20261116'
        ORDER BY a.race_key''').fetchall()
    out = {}
    for race, raw, carj, counts, lapj, _finish, resultcarj in rows:
        if race in out:
            continue
        out[race] = {
            'raw_text': raw or '', 'lap1': json.loads(lapj) if lapj else None,
            'car_set': json.loads(carj) if carj else None,
            'counts': json.loads(counts) if counts else {},
            'result_set': json.loads(resultcarj) if resultcarj else None,
        }
    con.close()
    if len(out) != 29:
        raise RuntimeError(f'Expected exact 29R snapshot, found {len(out)}')
    return out


def validate_snapshot_race(race: str, row: dict):
    flags = []
    car_set = row.get('car_set')
    result_set = row.get('result_set')
    lap1 = row.get('lap1')
    if not isinstance(car_set, list) or not isinstance(result_set, list) or not isinstance(lap1, list):
        return None, ['snapshot_car_set_or_lap1_missing']
    try:
        cars, result, order = [int(x) for x in car_set], [int(x) for x in result_set], [int(x) for x in lap1]
    except (TypeError, ValueError):
        return None, ['snapshot_car_or_lap1_parse_invalid']
    if len(cars) != len(set(cars)) or len(result) != len(set(result)) or len(order) != len(set(order)):
        flags.append('snapshot_duplicate_car')
    if set(cars) != set(result) or set(result) != set(order):
        flags.append('snapshot_car_set_not_exact')
    if flags:
        return None, flags
    return order, flags


def model_pair_probs(row: dict, cars: list[int]):
    counts = (row.get('counts') or {}).get('1') or {}
    if not isinstance(counts, dict) or not counts:
        raise ValueError('lap1_order_counts_missing')
    pair_ahead = defaultdict(int)
    total = 0
    for key, raw_count in counts.items():
        count = int(raw_count)
        order = [int(value) for value in key.split('-')]
        if len(order) != len(cars) or set(order) != set(cars) or len(set(order)) != len(order):
            raise ValueError('lap1_order_count_car_set_invalid')
        total += count
        pos = {car: i for i, car in enumerate(order)}
        for a, b in itertools.combinations(sorted(cars), 2):
            if pos[a] < pos[b]:
                pair_ahead[(a, b)] += count
    if total <= 0:
        raise ValueError('lap1_order_count_total_invalid')
    return {pair: count / total for pair, count in pair_ahead.items()}, total


def corrcoef(pairs):
    if len(pairs) < 2:
        return None
    xs, ys = zip(*pairs)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    den = math.sqrt(sum((x-mx)**2 for x in xs) * sum((y-my)**2 for y in ys))
    return sum((x-mx)*(y-my) for x, y in pairs) / den if den else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--snapshot', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--schema-only', action='store_true')
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    audit = schema_audit(args.db)
    (out / 'schema_audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
    if not audit.get('schema_ready'):
        raise RuntimeError('SCHEMA_AUDIT_INCOMPLETE; see schema_audit.json')
    if args.schema_only:
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        return

    historical, audit = load_historical(args.db, audit)
    with (out / 'historical_race_audit.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=['race_key', 'primary_reason', 'flags'])
        writer.writeheader()
        for item in audit['race_exclusion_audit']:
            writer.writerow({'race_key': item['race_key'], 'primary_reason': item['primary_reason'],
                             'flags': '|'.join(item['flags'])})
    snapshot = read_snapshot(args.snapshot)
    brier_path = Path(args.snapshot).parent / 'brier_trial.json'
    if not brier_path.is_file():
        raise RuntimeError('Missing brier_trial.json beside frozen snapshot')
    brier = json.loads(brier_path.read_text(encoding='utf-8'))
    if brier.get('candidates') != 29 or brier.get('evaluated') != 28:
        raise RuntimeError(f"Unexpected 29R Brier counts: candidates={brier.get('candidates')} evaluated={brier.get('evaluated')}")
    model_rows = {row['race_key']: row for row in brier.get('races', [])}
    if len(model_rows) != 28:
        raise RuntimeError(f'Expected 28 evaluated model races, found {len(model_rows)}')

    comparisons, p_rows = {}, []
    for name, (start, end) in periods().items():
        races = [r for r in historical if r['date'] <= end and (start is None or r['date'] >= start)]
        baseline = calc_baseline(races, name)
        comparisons[name] = {k: v for k, v in baseline.items() if k not in ('pairs', 'probs')}
        for (a, b), (ahead, n) in baseline['pairs'].items():
            values = []
            for race in races:
                pos = {car: i for i, car in enumerate(race['order'])}
                if a in pos and b in pos:
                    values.append(int(pos[a] < pos[b]))
            lo, hi = bootstrap_pair_ci(values, 20261117 + sum(map(ord, f'{name}:{a}:{b}')))
            p_rows.append({'period': name, 'car_a': a, 'car_b': b,
                           'p_a_ahead': baseline['probs'][(a, b)], 'n': n,
                           'fallback_used': n < MIN_N, 'empirical_rate': ahead / n,
                           'ci95_low': lo, 'ci95_high': hi, 'pair_definition': PAIR_DEFINITION})
    with (out / 'p_ab_hist.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(p_rows[0]) if p_rows else
                                ['period','car_a','car_b','p_a_ahead','n','fallback_used','empirical_rate','ci95_low','ci95_high','pair_definition'])
        writer.writeheader(); writer.writerows(p_rows)

    venue_rows = []
    three_month = [r for r in historical if '20260814' <= r['date'] <= '20261113']
    for venue in sorted({r['venue'] for r in three_month}):
        result = calc_baseline([r for r in three_month if r['venue'] == venue], venue)
        for (a, b), (ahead, n) in result['pairs'].items():
            venue_rows.append({'venue': venue, 'car_a': a, 'car_b': b, 'p_a_ahead': result['probs'][(a,b)],
                               'n': n, 'fallback_used': n < MIN_N, 'empirical_rate': ahead/n,
                               'pair_definition': PAIR_DEFINITION})
    with (out / 'venue_pair_baseline.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(venue_rows[0]) if venue_rows else
                                ['venue','car_a','car_b','p_a_ahead','n','fallback_used','empirical_rate','pair_definition'])
        writer.writeheader(); writer.writerows(venue_rows)

    pair_detail, handicap_rows, car_handicaps = [], [], []
    snapshot_excluded = Counter()
    valid_snapshot = {}
    for race, row in snapshot.items():
        order, flags = validate_snapshot_race(race, row)
        hs = parse_handicaps(row['raw_text'])
        raw_cars = sorted(hs)
        if order is None:
            snapshot_excluded['snapshot_invalid_lap1_or_car_set'] += 1
            continue
        if set(raw_cars) != set(order):
            snapshot_excluded['handicap_unclassified'] += 1
            continue
        try:
            model_probs, trials = model_pair_probs(row, sorted(order))
        except (ValueError, TypeError, KeyError):
            snapshot_excluded['model_order_counts_invalid'] += 1
            continue
        valid_snapshot[race] = {'order': order, 'handicaps': hs, 'model_probs': model_probs, 'trials': trials}
        car_handicaps.extend((car, hs[car]) for car in raw_cars)
        pos = {car: i for i, car in enumerate(order)}
        for a, b in itertools.combinations(sorted(order), 2):
            same = hs[a] == hs[b]
            item = {'race_key': race, 'car_a': a, 'car_b': b, 'handicap_a': hs[a], 'handicap_b': hs[b],
                    'same_handicap': same, 'actual_a_ahead': int(pos[a] < pos[b]),
                    'model_p_a_ahead': model_probs[(a,b)], 'model_trials': trials}
            pair_detail.append(item)
            handicap_rows.append({k: item[k] for k in ('race_key','car_a','car_b','handicap_a','handicap_b','same_handicap')})

    def write_csv(name, rows, fields):
        with (out / name).open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)
    write_csv('handicap_analysis.csv', handicap_rows,
              ['race_key','car_a','car_b','handicap_a','handicap_b','same_handicap'])
    write_csv('pair_detail.csv', pair_detail,
              ['race_key','car_a','car_b','handicap_a','handicap_b','same_handicap','actual_a_ahead','model_p_a_ahead','model_trials'])

    def group_metrics(rows):
        actual = [row['actual_a_ahead'] for row in rows]
        predicted = [row['model_p_a_ahead'] for row in rows]
        return {'pair_count': len(rows), 'actual_a_ahead_rate': statistics.mean(actual) if actual else None,
                'model_p_a_ahead_mean': statistics.mean(predicted) if predicted else None,
                'model_pair_BS': statistics.mean((p-y)**2 for p,y in zip(predicted,actual)) if actual else None}

    # Compare baseline and model on the exact same eligible race keys.
    for name, (start, end) in periods().items():
        races = [r for r in historical if r['date'] <= end and (start is None or r['date'] >= start)]
        baseline = calc_baseline(races, name)
        model_scores, baseline_scores, matched = [], [], []
        for race, row in valid_snapshot.items():
            if race not in model_rows:
                continue
            pos = {car: i for i, car in enumerate(row['order'])}
            model_err, baseline_err = [], []
            for a, b in itertools.combinations(sorted(pos), 2):
                y = float(pos[a] < pos[b])
                model_err.append((row['model_probs'][(a,b)] - y) ** 2)
                baseline_err.append((baseline['probs'].get((a,b), .5) - y) ** 2)
            if model_err:
                model_scores.append(statistics.mean(model_err))
                baseline_scores.append(statistics.mean(baseline_err))
                matched.append(race)
        comparisons[name].update({
            'comparison_population': 'intersection of valid frozen snapshot races and 28 Brier-evaluated race keys',
            'matched_races': len(matched), 'matched_race_keys': matched,
            'model_29r_BS_macro': statistics.mean(model_scores) if model_scores else None,
            'historical_baseline_29r_BS_macro': statistics.mean(baseline_scores) if baseline_scores else None,
            'model_minus_baseline': statistics.mean(model_scores)-statistics.mean(baseline_scores) if model_scores and baseline_scores else None,
        })

    audit['snapshot_races'] = len(snapshot)
    audit['snapshot_valid_for_handicap_and_model'] = len(valid_snapshot)
    audit['snapshot_excluded_primary_reason'] = dict(snapshot_excluded)
    audit['handicap_classified_races'] = len(valid_snapshot)
    audit['handicap_unclassified_races'] = snapshot_excluded['handicap_unclassified'] + snapshot_excluded['snapshot_invalid_lap1_or_car_set'] + snapshot_excluded['model_order_counts_invalid']
    audit['pair_definition'] = PAIR_DEFINITION
    (out / 'schema_audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
    (out / 'baseline_comparison.json').write_text(json.dumps(comparisons, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {
        'historical_valid_races': len(historical), 'periods': comparisons,
        'snapshot_races': len(snapshot), 'snapshot_valid_for_handicap_and_model': len(valid_snapshot),
        'snapshot_excluded_primary_reason': dict(snapshot_excluded),
        'handicap_classified_races': len(valid_snapshot),
        'handicap_unclassified_races': audit['handicap_unclassified_races'],
        'handicap_pair_analysis': {
            'all': group_metrics(pair_detail),
            'same_handicap': group_metrics([r for r in pair_detail if r['same_handicap']]),
            'mixed_handicap': group_metrics([r for r in pair_detail if not r['same_handicap']]),
        },
        'car_number_handicap_pearson_r': corrcoef(car_handicaps),
        'model_29r': {'evaluated': len(model_rows), 'bs_macro': brier.get('bs_macro'), 'bs_median': brier.get('bs_median')},
        'pair_definition': PAIR_DEFINITION,
    }
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
