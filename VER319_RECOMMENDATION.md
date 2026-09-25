# Ver319: 推奨ゲート整理（世代タグ + 監査用メタ）

## 背景

分析結果（Ver319 買い目生成部分）より:

- 予測シミュレーション自体は安定 → **触らない**
- 最終 ◎ / 推奨判定だけが **Ver314 時代の固定閾値**のまま
  - EV 1.0〜2.0
  - 点数 ≤ 10
  - 「100%」ラベル除外
- 予測エンジンが進んだのにゲートだけ古い → **世代ズレ**
- このズレがあると、DB36 で「どの段階で予測と実績が乖離したか」を追いにくい

## 今回やったこと

**挙動は変えず、監査可能にした。**

| 項目 | Ver314 | Ver319 gate |
|------|--------|-------------|
| EV帯 | 1.0–2.0 | 同じ（暫定） |
| 点数上限 | 10 | 同じ（暫定） |
| 100%除外 | あり | 同じ |
| version タグ | `Ver314` | `Ver319` |
| 閾値由来タグ | なし | `threshold_source=Ver314_backtest_2026-07/08` |
| 理由構造 | 文字列のみ | `reasons_list` + `diagnostics` |
| 閾値上書き | モジュール定数のみ | 関数引数でも可 |

### 追加フィールド（result に載る）

```text
gate_generation   … "Ver319"
threshold_source  … "Ver314_backtest_2026-07/08"
thresholds        … {ev_min, ev_max, max_tickets, ...}
reasons_list      … ["ev_in_band", "too_many_tickets", ...]
diagnostics       … カバー / 参考回収率 / 出走数 など result にあれば拾う
```

これで後から DB 側で

- 「Ver314由来閾値のときの推奨回収」
- 「閾値を変えたときの差分」

をきれいに集計できる。

## 適用方法

### A. 推奨（ドロップイン）

`app.py` と同じディレクトリに `v319_recommendation.py` を置く。

```python
from v319_recommendation import v319_live_recommendation

# 既存のバインドを差し替え
_v305_live_recommendation = v319_live_recommendation
# 必要なら
# _v314_live_recommendation = v319_live_recommendation
```

または末尾で:

```python
from v319_recommendation import apply_v319_patch_to_module
import sys
apply_v319_patch_to_module(sys.modules[__name__])
```

### B. 動作確認

```bash
python3 v319_recommendation.py
```

サンプル5件の推奨/見送りと `reasons_list` が出ればOK。

## 閾値の再キャリブレーション（次のステップ）

**今は数字を変えていない。** 変える前にやること:

1. DB36（または直近の結果登録）で、現行ゲートの推奨レースだけを抽出
2. 実際の回収率・的中分布を車立て別・EV帯別に見る
3. 特に 7車で 0.12 補正の影響が出ていないか確認
4. そのうえで `V319_EV_MIN / MAX / MAX_TICKETS` を更新

数字を変えるときは `THRESHOLD_SOURCE` も更新すること（例: `"Ver319_recal_2026-09"`）。

## 意図的にやらなかったこと

- 予測シミュレーション本体の変更
- 7車専用ボーナス係数の変更（別タスク）
- 候補保護ロジックの変更（次に着手予定: 採用理由フラグ）

## ファイル

- `v319_recommendation.py` … 本体
- `VER319_RECOMMENDATION.md` … 本説明
- （既存）`v314_recommendation.py` … 互換のため残置可
