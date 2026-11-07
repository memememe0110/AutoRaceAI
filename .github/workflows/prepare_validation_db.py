#!/usr/bin/env python3
"""Rebuild and verify AutoRaceAI's chunked SQLite backup from a checked-out DB branch."""
from __future__ import annotations
import argparse, hashlib, json, sqlite3
from pathlib import Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--db-root', required=True)
    ap.add_argument('--db-path', required=True, help='GITHUB_DB_PATH; relative to DB branch root')
    ap.add_argument('--output', required=True)
    args = ap.parse_args()
    root = Path(args.db_root).resolve()
    rel = args.db_path.lstrip('/')
    db_file = root / rel
    manifest_file = root / (rel + '.chunks.json')
    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    if manifest_file.exists():
        manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
        if manifest.get('format') != 'AutoRaceAI-sqlite-chunks-v4-atomic':
            raise SystemExit(f"Unsupported chunk manifest format: {manifest.get('format')}")
        parts = list(manifest.get('parts') or [])
        sizes = list(manifest.get('part_sizes') or [])
        shas = list(manifest.get('part_sha256') or [])
        if not parts or len(parts) != len(sizes) or len(parts) != len(shas):
            raise SystemExit('Chunk manifest arrays are missing or have different lengths')
        with out.open('wb') as f:
            for i, part_rel in enumerate(parts):
                part_path = root / str(part_rel).lstrip('/')
                if not part_path.is_file():
                    raise SystemExit(f'Missing DB chunk: {part_path}')
                block = part_path.read_bytes()
                if len(block) != int(sizes[i]):
                    raise SystemExit(f'Chunk size mismatch at {i+1}: {len(block)} != {sizes[i]}')
                if sha256(block) != str(shas[i]):
                    raise SystemExit(f'Chunk SHA256 mismatch at {i+1}')
                f.write(block)
        data_sha = sha256(out.read_bytes())
        if out.stat().st_size != int(manifest.get('size', -1)):
            raise SystemExit('Rebuilt DB size mismatch')
        if data_sha != str(manifest.get('sha256') or ''):
            raise SystemExit('Rebuilt DB SHA256 mismatch')
    elif db_file.is_file():
        out.write_bytes(db_file.read_bytes())
    else:
        raise SystemExit(f'DB file and chunk manifest not found: {db_file} / {manifest_file}')

    if out.read_bytes()[:16] != b'SQLite format 3\x00':
        raise SystemExit('Rebuilt file is not SQLite')
    with sqlite3.connect(f'file:{out.as_posix()}?mode=ro', uri=True) as con:
        con.execute('PRAGMA query_only=ON')
        integrity = con.execute('PRAGMA integrity_check').fetchone()[0]
        if str(integrity).lower() != 'ok':
            raise SystemExit(f'SQLite integrity_check failed: {integrity}')
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'result_laps', 'v222_prediction_restore', 'v231_prediction_history'}
        missing = sorted(required - tables)
        if missing:
            raise SystemExit('Required validation tables missing: ' + ', '.join(missing))
    print(json.dumps({'ok': True, 'db_path': str(out), 'size': out.stat().st_size, 'sha256': sha256(out.read_bytes())}))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
