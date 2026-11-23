#!/usr/bin/env python3
"""Read-only full row diff of v231_prediction_history across two immutable DB snapshots."""
from __future__ import annotations
import argparse, csv, hashlib, json, sqlite3
from collections import Counter
from pathlib import Path
from urllib.parse import quote

SENSITIVE_LARGE = {"raw_text", "payload", "result_blob", "compressed_payload", "pickle_blob"}

def cell_value(name, value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return {"bytes_len": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if name.lower() in SENSITIVE_LARGE:
        raw = str(value).encode("utf-8")
        return {"text_len": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if isinstance(value, str) and len(value) > 4096:
        raw = value.encode("utf-8")
        return {"text_len": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    return value

def open_readonly(path):
    uri = "file:" + quote(Path(path).resolve().as_posix(), safe="/:") + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con

def load_snapshot(path, info_path, expected_gen, expected_commit):
    info = json.loads(Path(info_path).read_text(encoding="utf-8"))
    if str(info.get("manifest_generation")) != str(expected_gen):
        raise SystemExit(f"GENERATION_MISMATCH:{expected_gen}")
    if str(info.get("commit_sha", "")).lower() != expected_commit.lower():
        raise SystemExit(f"COMMIT_MISMATCH:{expected_gen}")
    con = open_readonly(path)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if str(integrity).lower() != "ok":
            raise SystemExit(f"INTEGRITY_CHECK_FAILED:{expected_gen}:{integrity}")
        cols = [r[1] for r in con.execute("PRAGMA table_info(v231_prediction_history)")]
        if "history_id" not in cols:
            raise SystemExit(f"HISTORY_ID_MISSING:{expected_gen}")
        rows = {}
        cur = con.execute("SELECT * FROM v231_prediction_history")
        for raw in cur:
            d = dict(raw)
            hid = int(d["history_id"])
            if hid in rows:
                raise SystemExit(f"DUPLICATE_HISTORY_ID:{expected_gen}:{hid}")
            rows[hid] = d
    finally:
        con.close()
    return info, cols, rows, integrity

def normalized_row(row):
    return {k: cell_value(k, v) for k, v in row.items()}

def row_hash(row):
    raw = json.dumps(normalized_row(row), ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()

def write_csv(path, rows, fields):
    with Path(path).open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: json.dumps(v, ensure_ascii=False, sort_keys=True, default=str) if isinstance(v, (dict, list)) else v for k,v in row.items()})

def audit(db_a, info_a, gen_a, commit_a, db_b, info_b, gen_b, commit_b, roots_json, outdir):
    ia, ca, a, inta = load_snapshot(db_a, info_a, gen_a, commit_a)
    ib, cb, b, intb = load_snapshot(db_b, info_b, gen_b, commit_b)
    if ca != cb:
        raise SystemExit("SCHEMA_MISMATCH")
    roots = {int(x["source_history_id"]): x for x in json.loads(Path(roots_json).read_text(encoding="utf-8"))}
    ids_a, ids_b = set(a), set(b)
    deleted_ids = sorted(ids_a - ids_b)
    added_ids = sorted(ids_b - ids_a)
    common_ids = sorted(ids_a & ids_b)
    changed_ids = [hid for hid in common_ids if row_hash(a[hid]) != row_hash(b[hid])]
    out = Path(outdir); out.mkdir(parents=True, exist_ok=True)

    def detail(hid, row, state):
        d = {k: cell_value(k, v) for k,v in row.items()}
        d["history_id"] = hid
        d["diff_state"] = state
        d["is_external_root_37"] = hid in roots
        if hid in roots:
            d["external_root_race_key"] = roots[hid].get("race_key")
        return d

    deleted = [detail(hid, a[hid], "deleted_in_gen_b") for hid in deleted_ids]
    added = [detail(hid, b[hid], "added_in_gen_b") for hid in added_ids]
    changed = []
    for hid in changed_ids:
        changed.append({"history_id":hid,"gen_a_sha256":row_hash(a[hid]),"gen_b_sha256":row_hash(b[hid]),
                        "gen_a":normalized_row(a[hid]),"gen_b":normalized_row(b[hid])})
    base_fields = ["diff_state","history_id","is_external_root_37","external_root_race_key"] + ca
    write_csv(out/"deleted_history_rows.csv", deleted, list(dict.fromkeys(base_fields)))
    write_csv(out/"added_history_rows.csv", added, list(dict.fromkeys(base_fields)))
    write_csv(out/"changed_retained_rows.csv", changed, ["history_id","gen_a_sha256","gen_b_sha256","gen_a","gen_b"])

    def grouped(rows, field, transform=lambda x: str(x) if x is not None else "(NULL)"):
        c=Counter(transform(r.get(field)) for r in rows)
        return [{"value":k,"count":v} for k,v in sorted(c.items(), key=lambda x:(-x[1],x[0]))]
    for name, field in [("deleted_by_app_version.csv","app_version"),
                        ("deleted_by_prediction_time.csv","prediction_time"),
                        ("deleted_by_settings_hash.csv","settings_hash")]:
        write_csv(out/name, grouped(deleted,field), ["value","count"])
    write_csv(out/"deleted_by_race_date.csv",
              grouped(deleted,"race_key",lambda x: str(x)[:8] if x else "(NULL)"), ["value","count"])

    deleted_root_ids = sorted(set(deleted_ids) & set(roots))
    def date_only(x):
        return str(x)[:10] if x else "(NULL)"
    def time_bounds(rows):
        vals=sorted(str(r.get("prediction_time")) for r in rows if r.get("prediction_time"))
        return {"min": vals[0] if vals else None, "max": vals[-1] if vals else None}
    root_details=[]
    for hid in deleted_root_ids:
        row=next(r for r in deleted if int(r["history_id"])==hid)
        root_details.append({k:row.get(k) for k in ("history_id","race_key","app_version","prediction_time",
            "settings_hash","simulation_mode","seed","trials","venue_override","raw_text")})
    summary = {
        "gen_a": {"generation":ia.get("manifest_generation"),"commit_sha":ia.get("commit_sha"),
                  "saved_at":ia.get("manifest_saved_at"),"db_sha256":ia.get("db_sha256"),
                  "manifest_blob_sha":ia.get("manifest_blob_sha"),"row_count":len(a),"integrity_check":inta},
        "gen_b": {"generation":ib.get("manifest_generation"),"commit_sha":ib.get("commit_sha"),
                  "saved_at":ib.get("manifest_saved_at"),"db_sha256":ib.get("db_sha256"),
                  "manifest_blob_sha":ib.get("manifest_blob_sha"),"row_count":len(b),"integrity_check":intb},
        "schema_columns":ca,"deleted_count":len(deleted_ids),"added_count":len(added_ids),
        "retained_count":len(common_ids),"changed_retained_count":len(changed_ids),
        "deleted_history_id_min":min(deleted_ids) if deleted_ids else None,
        "deleted_history_id_max":max(deleted_ids) if deleted_ids else None,
        "external_roots_expected":len(roots),"external_roots_deleted":len(deleted_root_ids),
        "deleted_external_root_ids":deleted_root_ids,
        "deleted_external_root_details":root_details,
        "deleted_external_root_race_keys":[roots[x].get("race_key") for x in deleted_root_ids],
        "deleted_by_app_version":grouped(deleted,"app_version"),
        "retained_by_app_version":grouped([normalized_row(a[x]) for x in common_ids],"app_version"),
        "deleted_by_settings_hash":grouped(deleted,"settings_hash"),
        "deleted_by_simulation_mode":grouped(deleted,"simulation_mode"),
        "deleted_by_venue_override":grouped(deleted,"venue_override"),
        "deleted_by_prediction_date":grouped(deleted,"prediction_time",lambda x: date_only(x)),
        "deleted_prediction_time_bounds":time_bounds(deleted),
        "retained_prediction_time_bounds":time_bounds([a[x] for x in common_ids]),
        "deleted_by_race_date":grouped(deleted,"race_key",lambda x: str(x)[:8] if x else "(NULL)"),
        "database_open_mode":"ro","query_only":True,"pickle_loads_called":False,
        "large_text_and_blob_content_exported":False
    }
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return summary

if __name__ == "__main__":
    p=argparse.ArgumentParser()
    for x in ("db_a","info_a","gen_a","commit_a","db_b","info_b","gen_b","commit_b","roots_json","outdir"):
        p.add_argument("--"+x.replace("_","-"),required=True)
    a=p.parse_args()
    print(json.dumps(audit(a.db_a,a.info_a,a.gen_a,a.commit_a,a.db_b,a.info_b,a.gen_b,a.commit_b,a.roots_json,a.outdir),ensure_ascii=False,indent=2))
