#!/usr/bin/env python3
"""Resumable, per-race Ver326 Lap1 validation worker for GitHub Actions.

Results and progress are checkpointed to the checked-out DB branch after every race.
No production DB writes or learning/history updates are performed.
"""
from __future__ import annotations
import argparse, csv, json, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
import importlib.util

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('verify_ver330_validation', HERE / 'verify_ver330_validation.py')
verify = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verify
spec.loader.exec_module(verify)

FIELDS = [
    'race_key','car_count','actual_pattern','unique_pattern_count','pattern_rank',
    'pattern_count','pattern_prob','uniform_ratio','top1_prob','relative_lift',
    'observed_trials','rank_cutoff_25pct','rank_pass','uniform_pass','top1_pass',
    'relative_lift_pass','sim_trials_reported','ok','error'
]


def _file_sha256(path: Path):
    import hashlib
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def read_rows(path: Path):
    if not path.exists():
        return []
    with path.open('r', newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def write_rows(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def git_checkpoint(db_root: Path, branch: str, progress_path: Path, csv_path: Path, message: str):
    rel_progress = progress_path.relative_to(db_root).as_posix()
    rel_csv = csv_path.relative_to(db_root).as_posix()
    def run(args):
        p = subprocess.run(args, cwd=db_root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if p.returncode:
            raise RuntimeError(f"Git checkpoint failed ({' '.join(args)}): {p.stdout[-2500:]}")
        return p.stdout
    run(['git','config','user.name','AutoRaceAI Validation Bot'])
    run(['git','config','user.email','actions@users.noreply.github.com'])
    run(['git','add','--',rel_progress,rel_csv])
    status = subprocess.run(['git','diff','--cached','--quiet'], cwd=db_root)
    if status.returncode == 0:
        return
    run(['git','commit','-m',message[:180]])
    run(['git','push','origin',f'HEAD:refs/heads/{branch}'])


def load_target_set(db: Path):
    """Return the frozen common Ver326/Ver328 set and its Lap1-valid 377R subset."""
    with verify.ro_connect(db) as con:
        ptab=verify.prediction_table(con)
        actual_cond,actual_value=verify.result_lap1_condition(con)
        v326={str(r[0]) for r in con.execute(
            f'SELECT DISTINCT race_key FROM "{ptab}" WHERE app_version=?',('Ver326',)
        ).fetchall() if str(r[0] or '').strip()}
        v328={str(r[0]) for r in con.execute(
            f'SELECT DISTINCT race_key FROM "{ptab}" WHERE app_version=?',('Ver328',)
        ).fetchall() if str(r[0] or '').strip()}
        actual={str(r[0]) for r in con.execute(
            f'SELECT DISTINCT race_key FROM result_laps WHERE {actual_cond}',(actual_value,)
        ).fetchall() if str(r[0] or '').strip()}
    shared=v326 & v328
    missing_lap1=sorted(shared-actual)
    if set(missing_lap1) != set(verify.EXCLUDED_NO_LAP1):
        raise RuntimeError(f'Unexpected common-set Lap1 exclusions: {missing_lap1}')
    target=sorted((shared & actual)-set(verify.EXCLUDED_NO_LAP1))
    if len(target) != verify.EXPECTED_LAP1_COUNT:
        raise RuntimeError(f'Lap1 target mismatch: {len(target)} != {verify.EXPECTED_LAP1_COUNT}')
    return target, {'shared_prediction_races':len(shared),'missing_lap1_common':missing_lap1,
                    'target_count':len(target),'excluded_no_lap1':sorted(verify.EXCLUDED_NO_LAP1)}


def summary_from_rows(rows):
    ok = [r for r in rows if str(r.get('ok','')).lower() in ('true','1')]
    def nums(key):
        vals=[]
        for r in ok:
            try: vals.append(float(r[key]))
            except Exception: pass
        return vals
    def med(vals):
        return verify.median(vals)
    rank_cut = med(nums('rank_cutoff_25pct'))
    rank = med(nums('pattern_rank'))
    vals = {
        'n_races': len(ok),
        'pattern_rank': rank,
        'pattern_rank_cutoff_25pct_median': rank_cut,
        'uniform_ratio': med(nums('uniform_ratio')),
        'top1_prob': med(nums('top1_prob')),
        'relative_lift': med(nums('relative_lift')),
    }
    passes = {
        'pattern_rank': rank is not None and rank_cut is not None and rank <= rank_cut,
        'uniform_ratio': vals['uniform_ratio'] is not None and vals['uniform_ratio'] >= 50,
        'top1_prob': vals['top1_prob'] is not None and vals['top1_prob'] >= 0.02,
        'relative_lift': vals['relative_lift'] is not None and vals['relative_lift'] >= 0.20,
    }
    return {'median': vals, 'pass': passes, 'all_four_pass': all(passes.values())}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--db',required=True)
    ap.add_argument('--app',required=True)
    ap.add_argument('--db-root',required=True)
    ap.add_argument('--results-rel',required=True)
    ap.add_argument('--run-id',required=True)
    ap.add_argument('--branch',required=True)
    ap.add_argument('--max-races',type=int,default=50)
    ap.add_argument('--trials',type=int,default=20000)
    ap.add_argument('--seed',type=int,default=12345)
    ap.add_argument('--timeout',type=float,default=1800)
    a=ap.parse_args()
    if a.trials != 20000 or a.seed != 12345:
        raise SystemExit('Pre-registered settings are fixed: trials=20000, seed=12345')
    db=Path(a.db).resolve(); app=Path(a.app).resolve(); db_root=Path(a.db_root).resolve()
    result_dir=db_root / a.results_rel / a.run_id
    result_dir.mkdir(parents=True,exist_ok=True)
    csv_path=result_dir/'results.csv'; progress_path=result_dir/'progress.json'
    progress=read_json(progress_path, {})
    rows=read_rows(csv_path)
    # CSV is authoritative only for successful rows; errors remain visible but are retried on resume.
    done={str(r.get('race_key')) for r in rows if str(r.get('ok','')).lower() in ('true','1')}

    # 事前確認済みの共通セット定義を再現する。
    # 全予測履歴のうちLap1結果がないレース数は対象外開催を含むため、
    # Ver326/Ver328の共通集合に限定して「全返還1RだけがLap1なし」を検証する。
    common, contamination = load_target_set(db)

    started=str(progress.get('started_at') or now_iso())
    remaining=[k for k in common if k not in done]
    progress.update({
        'run_id':a.run_id,'phase':'Ver326 Lap1','status':'running',
        'total':verify.EXPECTED_LAP1_COUNT,'done_count':len(done),'failed_count':sum(1 for r in rows if str(r.get('ok','')).lower() not in ('true','1')),
        'current_race':'','started_at':started,'updated_at':now_iso(),
        'trials':a.trials,'seed':a.seed,'batch_limit':a.max_races,
        'db_sha256':_file_sha256(db),
        'target_check':contamination,
    })
    progress_path.write_text(json.dumps(progress,ensure_ascii=False,indent=2),encoding='utf-8')
    write_rows(csv_path,rows)
    git_checkpoint(db_root,a.branch,progress_path,csv_path,f'Ver330-A validation {a.run_id}: worker started')

    batch=remaining[:max(1,a.max_races)]
    for race_key in batch:
        progress.update({'current_race':race_key,'updated_at':now_iso(),'done_count':len(done)})
        progress_path.write_text(json.dumps(progress,ensure_ascii=False,indent=2),encoding='utf-8')
        git_checkpoint(db_root,a.branch,progress_path,csv_path,f'Ver330-A validation {a.run_id}: {len(done)}/{len(common)} start {race_key}')
        out_json=result_dir/'runs'/f'{len(done)+1:03d}.json'
        try:
            data=verify.run_one_race(app,db,race_key,'Ver326',a.trials,a.seed,out_json,a.timeout)
            if not data.get('validation_full_trials'):
                raise RuntimeError('validation_full_trials is false')
            if int(data.get('sim_trials') or 0) != a.trials:
                raise RuntimeError(f"sim_trials mismatch: {data.get('sim_trials')} != {a.trials}")
            with verify.ro_connect(db) as con:
                actual=verify.actual_lap_order(con,race_key)
            if not actual:
                raise RuntimeError('actual Lap1 order missing')
            metrics=verify.pattern_metrics(data.get('lap_order_counts',{}).get('1',{}),actual,a.trials)
            row={**metrics,'race_key':race_key,'sim_trials_reported':int(data.get('sim_trials') or 0),'ok':True,'error':''}
        except Exception as exc:
            row={'race_key':race_key,'ok':False,'error':f'{type(exc).__name__}: {exc}'}
            # Persist failure diagnostics and stop this workflow run. Rerun resumes and retries this race.
            rows=[r for r in rows if str(r.get('race_key')) != race_key]+[row]
            write_rows(csv_path,rows)
            progress.update({'status':'failed','current_race':race_key,'done_count':len(done),'failed_count':1,'last_error':row['error'],'updated_at':now_iso()})
            progress_path.write_text(json.dumps(progress,ensure_ascii=False,indent=2),encoding='utf-8')
            git_checkpoint(db_root,a.branch,progress_path,csv_path,f'Ver330-A validation {a.run_id}: failed {race_key}')
            raise
        rows=[r for r in rows if str(r.get('race_key')) != race_key]+[row]
        done.add(race_key)
        write_rows(csv_path,rows)
        elapsed=max(1,(datetime.now(timezone.utc)-datetime.fromisoformat(started.replace('Z','+00:00'))).total_seconds())
        avg=elapsed/len(done)
        progress.update({
            'status':'running','done_count':len(done),'failed_count':0,
            'current_race':'','last_completed_race':race_key,'updated_at':now_iso(),
            'elapsed_seconds':int(elapsed),'estimated_remaining_seconds':int(max(0,len(common)-len(done))*avg),
            'summary_so_far':summary_from_rows(rows),
        })
        progress_path.write_text(json.dumps(progress,ensure_ascii=False,indent=2),encoding='utf-8')
        git_checkpoint(db_root,a.branch,progress_path,csv_path,f'Ver330-A validation {a.run_id}: {len(done)}/{len(common)} {race_key}')
        print(f"CHECKPOINT {len(done)}/{len(common)} {race_key}",flush=True)

    if len(done) == len(common):
        final=summary_from_rows(rows)
        progress.update({'status':'completed','done_count':len(done),'current_race':'','finished_at':now_iso(),'updated_at':now_iso(),'final_summary':final})
    else:
        progress.update({'status':'running','done_count':len(done),'current_race':'','updated_at':now_iso()})
    progress_path.write_text(json.dumps(progress,ensure_ascii=False,indent=2),encoding='utf-8')
    write_rows(csv_path,rows)
    git_checkpoint(db_root,a.branch,progress_path,csv_path,f'Ver330-A validation {a.run_id}: batch complete {len(done)}/{len(common)}')
    print(json.dumps(progress,ensure_ascii=False),flush=True)

if __name__=='__main__':
    main()
