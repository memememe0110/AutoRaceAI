from pathlib import Path

import pandas as pd
import engine


ROOT = Path(__file__).resolve().parents[1]


def _simulation_source():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    start = source.index("def _v230_six_lap_simulation")
    end = source.index("\n\n# Ver330-A compare mode", start)
    return source, source[start:end]


def test_app_and_engine_are_valid_python():
    compile((ROOT / "app.py").read_text(encoding="utf-8"), "app.py", "exec")
    compile((ROOT / "engine.py").read_text(encoding="utf-8"), "engine.py", "exec")


def test_simulation_lap_count_controls_all_lap_outputs_and_transitions():
    source, fn = _simulation_source()
    assert '_lap_raw = (meta or {}).get("周回数", 6)' in fn
    assert 'if total_laps not in (6, 7, 8):' in fn
    assert 'for lap in range(1,total_laps+1):' in fn
    assert 'for _lap276 in range(1, total_laps + 1):' in fn
    assert 'for _lap276 in range(5, total_laps + 1):' in fn
    assert 'actual_lap_orders[:total_laps]' in fn
    assert 'int(lap) >= 6' in fn
    assert 'for lap in range(1,7):' not in fn
    helper_start = source.index('def _v272_late_chase_release')
    helper_end = source.index('\n\ndef ', helper_start + 5)
    helper = source[helper_start:helper_end]
    assert 'if lap_i < 5:' in helper
    assert 'lap_not_5_6' not in helper


def _projection_frame():
    return pd.DataFrame({
        "車": [1, 2, 3, 4],
        "選手名": ["A", "B", "C", "D"],
        "ハンデ": [0, 10, 20, 30],
        "ST予測": [0.12, 0.14, 0.16, 0.18],
        "スタート伸び指数": [0.7, 0.6, 0.5, 0.4],
        "改善後順位": [3, 1, 2, 4],
        "ゴール前伸び指数": [0.4, 0.8, 0.7, 0.3],
        "混戦突破適性": [0.5, 0.7, 0.6, 0.4],
    })


def test_six_lap_projection_is_backward_compatible():
    df = _projection_frame()
    pd.testing.assert_frame_equal(
        engine.v30_representative_lap_projection(df),
        engine.v30_representative_lap_projection(df, total_laps=6),
    )
    assert len(engine.v30_representative_lap_projection(df)) == 6


def test_eight_lap_projection_emits_all_eight_snapshots():
    result = engine.v30_representative_lap_projection(_projection_frame(), total_laps=8)
    assert len(result) == 8
    assert result["周回"].tolist() == [f"{i}周目" for i in range(1, 9)]


def test_invalid_projection_lap_count_falls_back_to_six():
    df = _projection_frame()
    pd.testing.assert_frame_equal(
        engine.v30_representative_lap_projection(df, total_laps="unknown"),
        engine.v30_representative_lap_projection(df, total_laps=6),
    )
