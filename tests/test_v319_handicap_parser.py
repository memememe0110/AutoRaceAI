from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
FIXTURE = ROOT / "tests" / "fixtures" / "sanyo_10r_handicap_regression.html"


def _load_app_functions():
    source = APP.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(APP))
    wanted = {
        "_v319_normalize_handicap",
        "_v319_norm_player_name",
        "_v319_td_text",
        "_v319_parse_sprace_car_rows",
        "_v319_parse_sprace_players",
        "_v319_html_to_text",
        "_v319_validate_card_players",
        "_v319_build_prediction_text_from_sprace",
        "_v319_synthesize_sprace_html_from_program",
    }
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in wanted
    ]
    missing = wanted - {node.name for node in nodes}
    assert not missing, f"app.py functions missing: {sorted(missing)}"
    ns = {
        "re": re,
        "engine": SimpleNamespace(DB_PATH=""),
        "_V319_LAST_CARD_FILL": {
            "source": "",
            "trial_default": 0,
            "hand_source_missing": 0,
            "hand_parse_failed": 0,
            "dev_default": 0,
            "cars": 0,
        },
    }
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(APP), "exec"), ns)
    return ns


def _row(car: int, name_tail: str, *, kessha: bool = False) -> str:
    marker = '<span class="alert">欠車</span>' if kessha else ""
    return (
        f'<td class="stcNameFull" param="{car}" '
        f'href="/autorace/SpPlayerDetail.do?playerCd={1000 + car}">'
        f'<strong>テスト選手{car}</strong>(山陽) {name_tail} {marker}</td>'
        f'<td>選手</td><td>3.40<br>0.081</td>'
    )


def test_sanyo_10r_fragment_preserves_all_eight_handicaps():
    ns = _load_app_functions()
    html = FIXTURE.read_text(encoding="utf-8")
    rows = ns["_v319_parse_sprace_car_rows"](html)
    assert [row["hand"] for row in rows] == ["0", "10", "10", "10", "10", "10", "10", "10"]


def test_handicap_without_st_is_parsed():
    ns = _load_app_functions()
    rows = ns["_v319_parse_sprace_car_rows"]("<table><tr>" + _row(2, "10m") + "</tr></table>")
    assert rows[0]["hand"] == "10"
    assert rows[0]["st"] == ""


def test_handicap_and_st_are_extracted_independently():
    ns = _load_app_functions()
    rows = ns["_v319_parse_sprace_car_rows"]("<table><tr>" + _row(2, "10m/ST0.15") + "</tr></table>")
    assert rows[0]["hand"] == "10"
    assert rows[0]["st"] == "0.15"


def test_existing_n_m_slash_st_format_still_works():
    ns = _load_app_functions()
    rows = ns["_v319_parse_sprace_car_rows"]("<table><tr>" + _row(3, "20m/ST0.21") + "</tr></table>")
    assert rows[0]["hand"] == "20"
    assert rows[0]["st"] == "0.21"


def test_missing_handicap_is_allowed_only_for_absent_row():
    ns = _load_app_functions()
    html = "<table><tr>" + _row(8, "", kessha=True) + "</tr></table>"
    rows = ns["_v319_parse_sprace_car_rows"](html)
    active_html = "<table><tr>" + _row(1, "0m") + "</tr></table>"
    active = ns["_v319_parse_sprace_car_rows"](active_html)
    assert rows[0]["hand"] is None
    assert rows[0]["kessha"] is True
    ns["_v319_validate_card_players"](active + rows, "山陽", 10)


def test_missing_handicap_for_active_starter_fails_validation():
    ns = _load_app_functions()
    html = "<table><tr>" + _row(2, "") + "</tr></table>"
    rows = ns["_v319_parse_sprace_car_rows"](html)
    try:
        ns["_v319_validate_card_players"](rows, "山陽", 10)
    except ValueError as exc:
        assert "ハンデ欠損" in str(exc)
    else:
        raise AssertionError("active starter with missing handicap must fail")
    assert ns["_V319_LAST_CARD_FILL"]["hand_parse_failed"] == 1


def test_prediction_text_builder_never_zero_fills_missing_handicap():
    ns = _load_app_functions()
    html = "<table><tr>" + _row(2, "") + "</tr></table>"
    try:
        ns["_v319_build_prediction_text_from_sprace"](
            html, "山陽", 10, "20261117", strict_trial=False
        )
    except ValueError as exc:
        assert "後段で解析できません" in str(exc)
    else:
        raise AssertionError("builder must reject missing handicap")


def test_program_rebuild_keeps_handicap_when_st_is_absent():
    ns = _load_app_functions()
    body = {"playerList": [{
        "carNo": 2, "playerCode": "1002", "playerName": "テスト選手2",
        "placeName": "山陽", "handicap": 10, "trialRunTime": "3.40",
        "startTime": None, "absent": None,
    }]}
    html = ns["_v319_synthesize_sprace_html_from_program"](
        body, "山陽", 10, "20261117"
    )
    rows = ns["_v319_parse_sprace_car_rows"](html)
    assert rows[0]["hand"] == "10"
    assert rows[0]["st"] == ""


def test_program_rebuild_rejects_missing_active_handicap():
    ns = _load_app_functions()
    body = {"playerList": [{
        "carNo": 2, "playerCode": "1002", "playerName": "テスト選手2",
        "placeName": "山陽", "handicap": None, "trialRunTime": "3.40",
        "absent": None,
    }]}
    try:
        ns["_v319_synthesize_sprace_html_from_program"](
            body, "山陽", 10, "20261117"
        )
    except ValueError as exc:
        assert "ハンデが取得元で欠損" in str(exc)
    else:
        raise AssertionError("Program rebuild must reject missing active handicap")
    assert ns["_V319_LAST_CARD_FILL"]["hand_source_missing"] == 1


def test_program_rebuild_keeps_missing_handicap_on_absent_row():
    ns = _load_app_functions()
    body = {"playerList": [{
        "carNo": 8, "playerCode": "1008", "playerName": "テスト選手8",
        "placeName": "山陽", "handicap": None, "trialRunTime": "",
        "absent": "1",
    }]}
    html = ns["_v319_synthesize_sprace_html_from_program"](
        body, "山陽", 10, "20261117"
    )
    rows = ns["_v319_parse_sprace_car_rows"](html)
    assert rows[0]["hand"] is None
    assert rows[0]["kessha"] is True
    assert ns["_V319_LAST_CARD_FILL"]["hand_source_missing"] == 1
