#!/usr/bin/env python3
"""Read-only verification and selective export of the immutable Gen675 release DB."""
import argparse, csv, hashlib, json, sqlite3
from pathlib import Path

EXPECTED_SHA256 = "d5e7c141ae0e66708a53b2e8ac4d7035d019aeaf98cba596bc83d9d2d128d7ac"
EXPECTED_HISTORY = 1365
EXPECTED_LAPS = 8196
VERSIONS = ("Ver322", "Ver323", "Ver324")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--db",required=True)
    p.add_argument("--roots",required=True)
    p.add_argument("--outdir",required=True)
    a=p.parse_args()
    db=Path(a.db); out=Path(a.outdir); out.mkdir(parents=True,exist_ok=True)
    h=hashlib.sha256()
    with db.open("rb") as f:
        for chunk in iter(lambda:f.read(8*1024*1024),b""): h.update(chunk)
    actual=h.hexdigest()
    if actual != EXPECTED_SHA256: raise SystemExit(f"DB_SHA256_MISMATCH:{actual}")
    uri="file:"+db.resolve().as_posix()+"?mode=ro"
    con=sqlite3.connect(uri,uri=True)
    con.row_factory=sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    try:
        integrity=con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity!="ok": raise SystemExit(f"INTEGRITY_FAILED:{integrity}")
        roots=json.loads(Path(a.roots).read_text(encoding="utf-8"))
        root_ids={int(x["source_history_id"]):x for x in roots}
        hcols=[r[1] for r in con.execute("PRAGMA table_info(v231_prediction_history)")]
        lcols=[r[1] for r in con.execute("PRAGMA table_info(v252_lap_prediction_snapshots)")]
        if "history_id" not in hcols or "app_version" not in hcols: raise SystemExit("HISTORY_SCHEMA_MISMATCH")
        if "app_version" not in lcols: raise SystemExit("LAP_SCHEMA_MISMATCH")
        marks=",".join("?" for _ in VERSIONS)
        hist=con.execute(f"SELECT * FROM v231_prediction_history WHERE app_version IN ({marks}) ORDER BY history_id",VERSIONS).fetchall()
        laps=con.execute(f"SELECT * FROM v252_lap_prediction_snapshots WHERE app_version IN ({marks}) ORDER BY snapshot_id",VERSIONS).fetchall()
        if len(hist)!=EXPECTED_HISTORY: raise SystemExit(f"HISTORY_COUNT_MISMATCH:{len(hist)}")
        if len(laps)!=EXPECTED_LAPS: raise SystemExit(f"LAP_COUNT_MISMATCH:{len(laps)}")
        hist_by_id={int(r["history_id"]):r for r in hist}
        missing=sorted(set(root_ids)-set(hist_by_id))
        if missing: raise SystemExit(f"ROOTS_MISSING:{missing}")
        root_checks=[]
        for hid, spec in sorted(root_ids.items()):
            row=hist_by_id[hid]
            raw=row["raw_text"]
            raw_bytes=raw if isinstance(raw,bytes) else str(raw).encode("utf-8")
            raw_sha=hashlib.sha256(raw_bytes).hexdigest()
            ok=(str(row["race_key"])==spec["race_key"] and raw_sha==spec["child_raw_text_sha256"]
                and str(row["seed"])==spec["child_seed"] and str(row["trials"])==spec["child_trials"])
            if not ok: raise SystemExit(f"ROOT_CONTENT_MISMATCH:{hid}")
            root_checks.append({"history_id":hid,"race_key":row["race_key"],"app_version":row["app_version"],
                                "seed":row["seed"],"trials":row["trials"],"raw_text_sha256":raw_sha,"match":True})
        def export(name, rows, cols):
            safe=[c for c in cols if c.lower() not in {"raw_text","payload","result_blob","compressed_payload","pickle_blob"}]
            with (out/name).open("w",newline="",encoding="utf-8-sig") as f:
                w=csv.DictWriter(f,fieldnames=safe,extrasaction="ignore"); w.writeheader()
                for row in rows: w.writerow({c:row[c] for c in safe})
        export("recovered_v231_history_Ver322-324.csv",hist,hcols)
        export("recovered_v252_lap_snapshots_Ver322-324.csv",laps,lcols)
        (out/"root37_verification.json").write_text(json.dumps(root_checks,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        summary={"db_sha256":actual,"db_size_bytes":db.stat().st_size,"integrity_check":integrity,
                 "database_open_mode":"ro","query_only":True,"pickle_loads_called":False,
                 "history_rows_Ver322_324":len(hist),"lap_snapshot_rows_Ver322_324":len(laps),
                 "root_ids_expected":len(root_ids),"root_ids_verified":len(root_checks),
                 "history_by_version":{v:sum(1 for r in hist if r["app_version"]==v) for v in VERSIONS},
                 "lap_by_version":{v:sum(1 for r in laps if r["app_version"]==v) for v in VERSIONS},
                 "raw_text_and_payload_exported":False,"separate_csv_exports_created":True}
        (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(summary,ensure_ascii=False,indent=2))
    finally: con.close()

if __name__=="__main__": main()
