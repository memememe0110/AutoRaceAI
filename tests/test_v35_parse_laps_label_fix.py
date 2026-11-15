import ast
import json
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((ROOT / "tests/fixtures/laps_label_cases.json").read_text(encoding="utf-8"))

def load_function(path, name, namespace):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert nodes, f"missing function {name} in {path}"
    code = ast.get_source_segment(source, nodes[-1])
    exec(compile(code, str(path), "exec"), namespace)
    return namespace[name]

converter = load_function(ROOT / "app.py", "_v319_autorace_result_to_text", {"re": re})
parser = load_function(ROOT / "engine.py", "_v35_parse_laps", {"re": re, "pd": pd})

def get_lap_lines(text):
    return [
        line for line in text.splitlines()
        if line.startswith("ゴール線\t")
        or (line.split("\t", 1)[0].endswith("周目") if "\t" in line else False)
    ]

def test_four_captured_api_cases_have_one_finish_line_and_no_empty_cars():
    expected_counts = {
        "hamamatsu_10r": 9,
        "hamamatsu_12r_void": 7,
        "kawaguchi_1r": 7,
        "sanyo_8r": 7,
    }
    for case in FIXTURES:
        text = converter(
            {"raceResult": [], "grandNoteList": case["grandNoteList"], "refundInfo": {}},
            "fixture", 1, "20261115"
        )
        lap_lines = get_lap_lines(text)
        assert len(lap_lines) == expected_counts[case["name"]], case["name"]
        assert sum(line.startswith("ゴール線\t") for line in lap_lines) == 1, case["name"]
        assert all(line.split("\t")[1:] for line in lap_lines), case["name"]
        assert all(
            car in {"1", "2", "3", "4", "5", "6", "7", "8"}
            for line in lap_lines for car in line.split("\t")[1:]
        ), case["name"]
        parsed = parser(text)
        assert not parsed.empty, case["name"]
        assert parsed["周回番号"].nunique() == expected_counts[case["name"]], case["name"]
        assert (parsed["周回番号"] == 99).sum() > 0, case["name"]

def test_void_case_uses_max_lap_with_valid_car_not_empty_notes():
    case = next(x for x in FIXTURES if x["name"] == "hamamatsu_12r_void")
    text = converter(
        {"raceResult": [], "grandNoteList": case["grandNoteList"], "refundInfo": {}},
        "浜松", 12, "20261102"
    )
    lap_lines = get_lap_lines(text)
    assert lap_lines[0].startswith("ゴール線\t4\t6\t7")
    assert len(lap_lines) == 7
    assert not any(line.startswith("8周") or line.startswith("9周") for line in lap_lines)

def test_parser_accepts_legacy_and_new_lap_labels():
    text = "グランドノート\n6周\t1\t2\t3\n5周目\t3\t2\t1\n払戻金"
    parsed = parser(text)
    assert set(parsed["周回番号"].tolist()) == {5, 6}
    assert set(parsed["周回"].tolist()) == {"6周", "5周目"}
