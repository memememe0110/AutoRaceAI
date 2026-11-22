#!/usr/bin/env python3
"""Read-only lookup of Issue #31 external rerun roots in a historical DB snapshot."""
import argparse, csv, hashlib, json, sqlite3
from pathlib import Path
from urllib.parse import quote

def raw_sha(value):
    if value is None or not str(value).strip():
        return ""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

def compare(parent, expected):
    if parent is None:
        return {"source_row_found": False}
    return {
        "source_row_found": True,
        "source_race_key": parent["race_key"],
        "source_app_version": parent["app_version"],
        "source_prediction_time": parent["prediction_time"],
        "source_settings_hash": parent["settings_hash"],
        "source_seed": parent["seed"],
        "source_trials": parent["trials"],
        "source_raw_text_sha256": raw_sha(parent["raw_text"]),
        "same_race": parent["race_key"] == expected["race_key"],
        "same_app_version": str(parent["app_version"]) == expected["child_app_version"],
        "same_settings_hash": str(parent["settings_hash"]) == expected["child_settings_hash"],
        "same_seed": str(parent["seed"]) == expected["child_seed"],
        "same_trials": str(parent["trials"]) == expected["child_trials"],
        "same_raw_text_sha256": raw_sha(parent["raw_text"]) == expected["child_raw_text_sha256"],
    }

def audit(db, info_path, expected_generation, expected_commit, reference_path, outdir):
    info=json.loads(Path(info_path).read_text(encoding="utf-8"))
    if str(info.get("manifest_generation")) != str(expected_generation):
        raise SystemExit("GENERATION_MISMATCH")
    if str(info.get("commit_sha","")).lower() != expected_commit.lower():
        raise SystemExit("DB_COMMIT_SHA_MISMATCH")
    refs=json.loads(Path(reference_path).read_text(encoding="utf-8"))
    ids=[int(x["source_history_id"]) for x in refs]
    uri="file:"+quote(Path(db).resolve().as_posix(),safe="/:")+"?mode=ro"
    con=sqlite3.connect(uri,uri=True); con.row_factory=sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    try:
        integrity=con.execute("PRAGMA integrity_check").fetchone()[0]
        if str(integrity).lower()!="ok": raise SystemExit("INTEGRITY_CHECK_FAILED")
        cols={r[1] for r in con.execute("PRAGMA table_info(v231_prediction_history)")}
        required={"history_id","race_key","app_version","settings_hash","prediction_time","trials","seed","raw_text"}
        if not required<=cols: raise SystemExit("HISTORY_COLUMNS_MISSING")
        found={int(r["history_id"]):dict(r) for r in con.execute(
            "SELECT history_id,race_key,app_version,settings_hash,prediction_time,trials,seed,raw_text "
            "FROM v231_prediction_history WHERE history_id IN ("+",".join("?" for _ in ids)+")",ids)}
        rows=[]
        for e in refs:
            p=found.get(int(e["source_history_id"]))
            rows.append({"race_key":e["race_key"],"source_history_id":e["source_history_id"],
                         "child_app_version":e["child_app_version"],"child_settings_hash":e["child_settings_hash"],
                         "child_seed":e["child_seed"],"child_trials":e["child_trials"],
                         "child_raw_text_sha256":e["child_raw_text_sha256"],**compare(p,e)})
    finally:
        con.close()
    out=Path(outdir); out.mkdir(parents=True,exist_ok=True)
    fields=list(dict.fromkeys(k for row in rows for k in row))
    with (out/"external_roots_historical.csv").open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    checks=("same_race","same_app_version","same_settings_hash","same_seed","same_trials","same_raw_text_sha256")
    summary={"db_generation":info.get("manifest_generation"),"db_commit_sha":info.get("commit_sha"),
             "db_saved_at":info.get("manifest_saved_at"),"integrity_check":integrity,
             "external_roots":len(refs),"source_rows_found":sum(r.get("source_row_found",False) for r in rows),
             "source_rows_missing":sum(not r.get("source_row_found",False) for r in rows),
             "matches":{k:sum(r.get(k) is True for r in rows) for k in checks},
             "pickle_loads_called":False,"database_open_mode":"ro"}
    (out/"external_roots_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return summary

if __name__=="__main__":
    p=argparse.ArgumentParser()
    for x in ("db","db_info","expected_generation","expected_commit","reference_json","outdir"):
        p.add_argument("--"+x.replace("_","-"),required=True)
    a=p.parse_args()
    print(json.dumps(audit(a.db,a.db_info,a.expected_generation,a.expected_commit,a.reference_json,a.outdir),ensure_ascii=False,indent=2))
