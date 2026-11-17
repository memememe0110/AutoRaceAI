#!/usr/bin/env python3
"""Rebuild and verify a read-only pinned AutoRaceAI DB snapshot."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY', 'memememe0110/AutoRaceAI'))
    ap.add_argument('--branch', default='autorace-db')
    ap.add_argument('--db-name', default='autorace_players.sqlite3')
    ap.add_argument('--expected-commit', default='20c67a8450ea72d219c19601319faf558a2f93c1')
    ap.add_argument('--out', required=True)
    ap.add_argument('--info-out', required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        raise SystemExit('GH_TOKEN/GITHUB_TOKEN is required')

    api = f'https://api.github.com/repos/{a.repo}/branches/{a.branch}'
    req = urllib.request.Request(api, headers={
        'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(req, timeout=30) as response:
        commit = json.load(response)['commit']['sha']
    if a.expected_commit and commit != a.expected_commit:
        raise SystemExit(f'DB branch moved: expected {a.expected_commit}, got {commit}')
    pinned_at = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    base = f'https://raw.githubusercontent.com/{a.repo}/{commit}/'

    def fetch(url: str, dest: Path) -> None:
        request = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(request, timeout=120) as response, dest.open('wb') as target:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                target.write(block)

    manifest_path = out.parent / 'db-manifest.json'
    fetch(base + a.db_name + '.chunks.json', manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('format') != 'AutoRaceAI-sqlite-chunks-v4-atomic':
        raise SystemExit(f"Unexpected manifest format: {manifest.get('format')}")
    parts, sizes, shas = manifest['parts'], manifest['part_sizes'], manifest['part_sha256']
    if not (len(parts) == len(sizes) == len(shas)):
        raise SystemExit('Manifest arrays have different lengths')

    digest = hashlib.sha256()
    total = 0
    with out.open('wb') as target:
        for index, (part, expected_size, expected_sha) in enumerate(zip(parts, sizes, shas), 1):
            chunk_path = out.parent / Path(part).name
            fetch(base + part, chunk_path)
            data = chunk_path.read_bytes()
            if len(data) != int(expected_size):
                raise SystemExit(f'Chunk {index} size mismatch')
            if hashlib.sha256(data).hexdigest() != expected_sha:
                raise SystemExit(f'Chunk {index} SHA256 mismatch')
            target.write(data)
            digest.update(data)
            total += len(data)
            chunk_path.unlink()
            print(f'CHUNK_OK {index}/{len(parts)}', flush=True)
    if total != int(manifest['size']) or digest.hexdigest() != manifest['sha256']:
        raise SystemExit('Rebuilt DB size/SHA256 mismatch')

    uri = f'file:{out.resolve()}?mode=ro'
    with sqlite3.connect(uri, uri=True) as con:
        con.execute('PRAGMA query_only=ON')
        integrity = con.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise SystemExit(f'SQLite integrity_check failed: {integrity}')
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'result_laps', 'result_entries', 'v222_prediction_restore', 'v231_prediction_history'}
        missing = sorted(required - tables)
        if missing:
            raise SystemExit(f'Required DB tables missing: {missing}')
    info = {
        'db_branch': a.branch, 'db_commit': commit, 'db_pin_time_utc': pinned_at,
        'db_sha256': digest.hexdigest(), 'db_size': total,
        'manifest_format': manifest['format'], 'parts': len(parts),
        'integrity_check': integrity, 'required_tables': 'PASS',
    }
    info_path = Path(a.info_out)
    info_path.parent.mkdir(parents=True, exist_ok=True)
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(info, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
