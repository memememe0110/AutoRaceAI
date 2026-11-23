#!/usr/bin/env python3
"""Reproduce the Gen675->Gen676 old-version cleanup on disposable copies only."""
from __future__ import annotations
import argparse, hashlib, json, shutil, sqlite3, tempfile
from pathlib import Path

EXPECTED_SHA = "d5e7c141ae0e66708a53b2e8ac4d7035d019aeaf98cba596bc83d9d2d128d7ac"
VERSIONS = ("Ver322", "Ver323", "Ver324")
TABLES = ("v231_prediction_history", "v252_lap_prediction_snapshots")
EXPECTED = {"v231_prediction_history": 1365, "v252_lap_prediction_snapshots": 8196}

def ro(path):
    c = sqlite3.connect("file:" + Path(path).resolve().as_posix() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    return c

def integrity(c):
    value = c.execute("PRAGMA integrity_check").fetchone()[0]
    if value != "ok":
        raise RuntimeError("integrity_check failed: " + str(value))
    return value

def digest_rows(c, table):
    cols = [r[1] for r in c.execute('PRAGMA table_info("' + table + '")')]
    h = hashlib.sha256()
    count = 0
    for row in c.execute('SELECT * FROM "' + table + '" ORDER BY rowid'):
        obj = []
        for v in row:
            if isinstance(v, bytes):
                obj.append({"bytes_sha256": hashlib.sha256(v).hexdigest(), "length": len(v)})
            else:
                obj.append(v)
        h.update(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str,
                            separators=(",", ":")).encode("utf-8") + b"\n")
        count += 1
    return {"count": count, "sha256": h.hexdigest(), "columns": cols}

def table_exists(c, name):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

def main():
    p = argparse.ArgumentParser()
    for x in ("gen675", "gen676", "roots", "outdir"):
        p.add_argument("--" + x, required=True)
    a = p.parse_args()
    g675, g676 = Path(a.gen675), Path(a.gen676)
    out = Path(a.outdir); out.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(g675.read_bytes()).hexdigest()
    if sha != EXPECTED_SHA:
        raise SystemExit("GEN675_SHA256_MISMATCH:" + sha)
    roots = json.loads(Path(a.roots).read_text(encoding="utf-8"))
    root_ids = {int(x["source_history_id"]) for x in roots}
    if len(root_ids) != 37:
        raise SystemExit("ROOT_DEFINITION_COUNT_MISMATCH")

    src = ro(g675); target = ro(g676)
    try:
        integrity(src); integrity(target)
        for t in TABLES:
            if not table_exists(src,t) or not table_exists(target,t):
                raise SystemExit("MISSING_TABLE:" + t)
        original = {t: digest_rows(src,t) for t in TABLES}
        expected_target = {t: digest_rows(target,t) for t in TABLES}
        selected = {}
        for t in TABLES:
            selected[t] = src.execute(
                'SELECT COUNT(*) FROM "' + t + '" WHERE app_version IN (?,?,?)', VERSIONS
            ).fetchone()[0]
            if selected[t] != EXPECTED[t]:
                raise SystemExit(f"EXTRACT_COUNT_MISMATCH:{t}:{selected[t]}")
        root_found = src.execute(
            'SELECT COUNT(*) FROM v231_prediction_history WHERE history_id IN (' +
            ",".join("?" for _ in root_ids) + ")", tuple(sorted(root_ids)
        ).fetchone()[0]
        if root_found != 37:
            raise SystemExit("ROOTS_NOT_ALL_PRESENT_IN_GEN675")

        with tempfile.TemporaryDirectory(prefix="issue31-repro-") as td:
            td = Path(td)
            stage1 = td / "stage1.sqlite3"
            shutil.copy2(g675, stage1)
            w = sqlite3.connect(stage1)
            w.execute("PRAGMA foreign_keys=OFF")
            try:
                # Stage 1: archive rows to temporary archive tables, then delete from main tables.
                for t, ar in ((TABLES[0], "v319_prediction_history_archive"),
                              (TABLES[1], "v319_lap_snapshots_archive")):
                    if table_exists(w, ar):
                        raise SystemExit("ARCHIVE_TABLE_ALREADY_EXISTS_IN_COPY:" + ar)
                    w.execute(f'CREATE TABLE "{ar}" AS SELECT * FROM "{t}" WHERE 0')
                    w.execute(f'INSERT INTO "{ar}" SELECT * FROM "{t}" WHERE app_version IN (?,?,?)', VERSIONS)
                    w.execute(f'DELETE FROM "{t}" WHERE app_version IN (?,?,?)', VERSIONS)
                w.commit()
                stage1_counts = {t: digest_rows(w,t) for t in TABLES}
                archive_counts = {
                    "v319_prediction_history_archive": w.execute("SELECT COUNT(*) FROM v319_prediction_history_archive").fetchone()[0],
                    "v319_lap_snapshots_archive": w.execute("SELECT COUNT(*) FROM v319_lap_snapshots_archive").fetchone()[0],
                }
                if archive_counts["v319_prediction_history_archive"] != EXPECTED[TABLES[0]]:
                    raise SystemExit("STAGE1_HISTORY_ARCHIVE_COUNT_MISMATCH")
                if archive_counts["v319_lap_snapshots_archive"] != EXPECTED[TABLES[1]]:
                    raise SystemExit("STAGE1_LAP_ARCHIVE_COUNT_MISMATCH")
                stage1.close()

                # Stage 2: export archive tables to a separate SQLite, drop from main, VACUUM.
                stage2 = td / "stage2.sqlite3"
                shutil.copy2(stage1, stage2)
                main_db = sqlite3.connect(stage2)
                archive_db = sqlite3.connect(out / "old_versions_archive_reproduction.sqlite3")
                try:
                    for src_table, dst_table in (
                        ("v319_prediction_history_archive","v231_prediction_history"),
                        ("v319_lap_snapshots_archive","v252_lap_prediction_snapshots")
                    ):
                        archive_db.execute(f'CREATE TABLE "{dst_table}" AS SELECT * FROM main."{src_table}"')
                        main_db.execute(f'DROP TABLE "{src_table}"')
                    archive_db.commit()
                    main_db.commit()
                    main_db.execute("VACUUM")
                    stage2_main = {t: digest_rows(main_db,t) for t in TABLES}
                    stage2_archive = {t: digest_rows(archive_db,t) for t in TABLES}
                    if stage2_archive[TABLES[0]]["count"] != EXPECTED[TABLES[0]] or stage2_archive[TABLES[1]]["count"] != EXPECTED[TABLES[1]]:
                        raise SystemExit("STAGE2_EXPORTED_ARCHIVE_COUNT_MISMATCH")
                    for t in TABLES:
                        if stage2_main[t] != expected_target[t]:
                            raise SystemExit("GEN676_TARGET_TABLE_MISMATCH:" + t)
                        if stage2_archive[t]["count"] != selected[t]:
                            raise SystemExit("ARCHIVE_ROW_COUNT_MISMATCH:" + t)
                    integrity(main_db); integrity(archive_db)
                    # Root IDs must be present in the exported Ver323 history archive.
                    q = "SELECT COUNT(*) FROM v231_prediction_history WHERE history_id IN (" + ",".join("?" for _ in root_ids) + ")"
                    exported_roots = archive_db.execute(q, tuple(sorted(root_ids))).fetchone()[0]
                    if exported_roots != 37:
                        raise SystemExit("EXPORTED_ROOTS_MISMATCH")
                    result = {
                        "gen675_sha256": sha, "gen675_integrity_check": "ok", "gen676_integrity_check": "ok",
                        "gen675_open_mode": "ro", "gen676_open_mode": "ro", "query_only": True,
                        "pickle_loads_called": False, "selected_versions": list(VERSIONS),
                        "stage1": {"description":"archive selected rows then delete from main tables",
                                   "archive_counts": archive_counts, "main_table_counts": stage1_counts},
                        "stage2": {"description":"export archive tables to separate SQLite, drop archive tables, VACUUM",
                                   "main_table_matches_gen676": True, "main_tables": stage2_main,
                                   "exported_archive_tables": stage2_archive, "exported_root_ids": exported_roots,
                                   "main_integrity_check":"ok", "archive_integrity_check":"ok"},
                        "expected_deleted": EXPECTED, "gen675_selected": selected,
                        "gen675_original_tables": original, "gen676_tables": expected_target,
                        "gen675_root_ids_found": root_found,
                        "archive_file":"old_versions_archive_reproduction.sqlite3",
                        "production_db_touched": False, "gen675_gen676_modified": False
                    }
                    (out/"reproduction_summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
                    print(json.dumps(result,ensure_ascii=False,indent=2))
                finally:
                    main_db.close(); archive_db.close()
            finally:
                try: w.close()
                except Exception: pass
    finally:
        src.close(); target.close()

if __name__ == "__main__":
    main()
