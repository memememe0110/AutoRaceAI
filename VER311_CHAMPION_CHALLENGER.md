# AutoRaceAI Ver311: Champion–Challenger model state

## What changed

Ver311 introduces two selectable model paths for normal predictions. **Champion（Ver305近似・継続学習）** starts from `ver305_champion_state.json`, a state recovered from the earliest Ver306 snapshot dated 2026-08-31, then advances its own base, heat, and auxiliary weights after each newly registered result using the existing Ver305-era learning formulas. **Challenger（最新学習状態）** clears the override and uses the current adaptive-learning state from the database.

The selector is in the Streamlit sidebar under **モデル方式**. The selected mode is saved in prediction metadata as `model_mode`, together with the champion source description when applicable. The existing historical records and version comparison data are not deleted or overwritten.

## Reproducibility behavior

Champion state activation occurs immediately before the normal prediction engine call and is cleared after the simulation stage. The rolling Champion state is persisted separately in `ver305_champion_state.json`; it is not written back to the Challenger learning database. Existing restored-history resimulation continues to use the snapshot state explicitly stored for that historical run, so historical records remain independently reproducible. Duplicate registrations and replacement registrations do not advance the rolling Champion.

## Validation performed

`python3 -m py_compile engine.py app.py` passed. A direct smoke test successfully loaded the champion state, activated the override, returned the expected weight set, and cleared the override.

## Files

- `app.py`: Ver311 version metadata, sidebar selector, and prediction-path integration.
- `engine.py`: champion state loader and model override activation helpers.
- `ver305_champion_state.json`: fixed champion model state.
- `autorace_players_complete.sqlite3`: existing full database is intentionally not modified by this change.

## Important limitation

This change establishes the rolling Ver305-style Champion path and the latest-learning Challenger path. The rolling update currently synchronizes the normal ten weights, five heat weights, and six auxiliary factors from the result-registration evidence; final accuracy comparison still requires running the same evaluation/resimulation workload against both modes and comparing the resulting historical metrics. No result labels are artificially locked.

## Git

Commit: `f5503bc` (`Implement Ver311 champion challenger model state`)
Branch: `ver310-diagnostic-output`
