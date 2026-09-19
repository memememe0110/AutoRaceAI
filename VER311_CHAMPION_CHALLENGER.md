# AutoRaceAI Ver312: Champion–Challenger model state

## What changed

Ver312 introduces two selectable model paths for normal predictions. **Champion（Ver305近似・固定）** loads `ver305_champion_state.json`, a fixed state recovered from the earliest Ver306 snapshot dated 2026-08-31. **Challenger（最新学習状態）** clears the override and uses the current adaptive-learning state from the database.

The selector is in the Streamlit sidebar under **モデル方式**. The selected mode is saved in prediction metadata as `model_mode`, together with the champion source description when applicable. The existing historical records and version comparison data are not deleted or overwritten.

## Reproducibility behavior

Champion state activation occurs immediately before the normal prediction engine call and is cleared after the simulation stage. The state is not written back to the learning database. Existing restored-history resimulation continues to use the snapshot state explicitly stored for that historical run, so historical records remain independently reproducible.

## Validation performed

`python3 -m py_compile engine.py app.py` passed. A direct smoke test successfully loaded the champion state, activated the override, returned the expected weight set, and cleared the override.

## Files

- `app.py`: Ver312 version metadata, sidebar selector, and prediction-path integration.
- `engine.py`: champion state loader and model override activation helpers.
- `ver305_champion_state.json`: fixed champion model state.
- `autorace_players_complete.sqlite3`: existing full database is intentionally not modified by this change.

## Important limitation

Ver312 keeps the fixed Ver305-era Champion state as the starting point, then updates and persists the Champion state after each learning-eligible result registration. The update captures the same post-learning state used by the Ver305-era learning path, while post-start accident / learning-excluded races do not advance Champion.

## Git

Commit: `f5503bc` (`Implement Ver312 champion challenger model state`)
Branch: `ver310-diagnostic-output`

