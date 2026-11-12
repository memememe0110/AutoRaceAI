"""Extract the card-building closure from a pinned app.py via AST (no app.py execution)."""
import ast, hashlib, builtins, json

SEEDS = [
    "_v319_fetch_sprace_html", "_v319_parse_sprace_car_rows", "_v319_parse_sprace_players",
    "_v319_validate_card_players", "_v319_build_prediction_text_from_sprace",
    "_v319_detect_kessha_from_players", "_v319_detect_kessha_from_card_text",
    "_v319_autorace_other_race_info", "_v319_other_info_to_meta_lines",
    "_v319_autorace_program_body", "_v319_program_players_from_body",
    "_v319_synthesize_sprace_html_from_program", "_v319_card_fill_note",
    "_v319_autorace_api_post",
]
FORBIDDEN = {"st", "engine"}  # closure must not touch streamlit or the engine
PRELUDE = (
    "import re, json, os, sys, time, math, html, hashlib, random, sqlite3, threading, unicodedata, datetime\n"
    "import urllib.request, urllib.parse, urllib.error, http.cookiejar\n"
    "from pathlib import Path\nfrom datetime import date, timedelta, timezone\n"
    "import time as time_module\nimport pandas as pd\nimport numpy as np\n"
)

def _top_level(tree):
    defs, assigns = {}, {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defs[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    assigns[t.id] = node   # last assignment wins, like runtime
    return defs, assigns

def _names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

def extract(app_path: str):
    src = open(app_path, encoding="utf-8").read()
    tree = ast.parse(src)
    defs, assigns = _top_level(tree)
    need, todo = set(), list(SEEDS)
    while todo:
        n = todo.pop()
        if n in need:
            continue
        node = defs.get(n) or assigns.get(n)
        if node is None:
            continue
        need.add(n)
        for a in ast.walk(node):
            if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id in FORBIDDEN:
                raise RuntimeError(f"closure member {n} uses {a.value.id}.{a.attr}")
        for m in _names(node):
            if (m in defs or m in assigns) and m not in need:
                todo.append(m)
    nodes = sorted({id(defs.get(n) or assigns.get(n)): (defs.get(n) or assigns.get(n)) for n in need}.values(),
                   key=lambda x: x.lineno)
    manifest = {"app_sha256": hashlib.sha256(src.encode("utf-8")).hexdigest(), "members": []}
    parts = [PRELUDE]
    for node in nodes:
        seg = ast.get_source_segment(src, node)
        name = getattr(node, "name", None) or ",".join(t.id for t in (node.targets if isinstance(node, ast.Assign) else [node.target]) if isinstance(t, ast.Name))
        manifest["members"].append({"name": name, "line": node.lineno,
                                    "sha256": hashlib.sha256(seg.encode("utf-8")).hexdigest()})
        parts.append(seg)
    code = "\n\n".join(parts) + "\n"
    manifest["closure_sha256"] = hashlib.sha256(code.encode("utf-8")).hexdigest()
    return code, manifest

def load(app_path: str):
    code, manifest = extract(app_path)
    ns = {"__name__": "pc_card_closure"}
    exec(compile(code, "pc_card_closure", "exec"), ns)
    missing = sorted(n for n in SEEDS if n not in ns)
    if missing:
        raise RuntimeError(f"missing seeds: {missing}")
    return ns, manifest

if __name__ == "__main__":
    import sys
    code, mf = extract(sys.argv[1])
    print(json.dumps({"n": len(mf["members"]), "members": [(m["name"], m["line"]) for m in mf["members"]]}, ensure_ascii=False))
