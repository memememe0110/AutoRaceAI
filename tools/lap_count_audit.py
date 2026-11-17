"""Read-only audit: lap simulation is hardcoded to 6.

Two separate paths:

1. Production finish probabilities
   ver16_run_prediction -> run_model -> simulate()
   simulate() is a vectorized finish model. It has no lap loop and
   does not take 周回数.

2. Lap-order simulation used by diagnostics / Ver330
   app._v230_six_lap_simulation(df, bets, entries, meta, trials, seed)
   It receives meta (which can contain 周回数) but allocates and loops
   only laps 1..6:
     lap_order_counts = {lap: {} for lap in range(1, 7)}
     for lap in range(1, 7):

Display helper engine.v30_representative_lap_projection also loops
range(2, 7) and is labeled as a 6-lap representative path.

So an 8-lap race still produces only 1-6 lap snapshots. Actual
result_laps for that race can contain 7周目/8周目, which this sim
never emits.
"""
from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _Dummy:
    def __getattr__(self, name):
        return self

    def __call__(self, *a, **k):
        if a and callable(a[0]):
            return a[0]
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False


class _ST(types.ModuleType):
    def __getattr__(self, name):
        d = _Dummy()
        setattr(self, name, d)
        return d


def _install_streamlit():
    if "streamlit" in sys.modules:
        return
    st = _ST("streamlit")
    st.session_state = {}
    st.query_params = {}
    st.sidebar = _Dummy()
    comps = _ST("streamlit.components")
    v1 = _ST("streamlit.components.v1")
    st.components = comps
    comps.v1 = v1
    sys.modules["streamlit"] = st
    sys.modules["streamlit.components"] = comps
    sys.modules["streamlit.components.v1"] = v1


_install_streamlit()
import app  # noqa: E402
import engine  # noqa: E402


def test_production_simulate_has_no_lap_loop():
    src = inspect.getsource(engine.simulate)
    assert "for lap in range" not in src
    sig = inspect.signature(engine.simulate)
    assert "laps" not in sig.parameters
    assert "total_laps" not in sig.parameters


def test_six_lap_sim_ignores_meta_laps():
    src = inspect.getsource(app._v230_six_lap_simulation)
    assert "for lap in range(1, 7)" in src or "for lap in range(1,7)" in src
    assert "range(1, 7)" in src or "range(1,7)" in src
    # meta is an argument, but 周回数 is not used to size the loop
    assert "周回数" not in src


def test_display_projection_is_six_laps():
    src = inspect.getsource(engine.v30_representative_lap_projection)
    assert "for lap in range(2, 7)" in src


if __name__ == "__main__":
    test_production_simulate_has_no_lap_loop()
    test_six_lap_sim_ignores_meta_laps()
    test_display_projection_is_six_laps()
    print("lap_count_audit: 6-lap hardcode confirmed")
