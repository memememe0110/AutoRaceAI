# Ver314: 推奨ロジック（回収率優先）

## 背景
- Ver305 推奨回収 272% は少数サンプル＋大的中1本に依存
- Ver313 推奨回収 55.6%（全体 57.4% より低い）
- 実績分析: **高EV・「回収率100%候補」は逆指標**。低EV帯（1.0〜2.0）が安定

## 変更内容
推奨条件を次に変更（`v314_recommendation.py`）:

1. **最大EV が 1.0 以上 2.0 以下**
2. **買い目点数 ≤ 10**
3. **元判定に「100%」を含む場合は推奨しない**

◎印の有無は必須にしない（低EV帯全体を推奨候補にする）。

## 過去データ上の想定回収
| 期間相当 | 現行推奨ROI | Ver314相当ルール |
|----------|-------------|------------------|
| Ver305 CSV | 272% (6R) | 約102% (21R, EV帯+点数) |
| Ver313 CSV | 55.6% (4R) | 約111% (29R) |

→ Ver313比で **+45〜55pt** 程度を狙う。305の272%超えは目標にしない。

## 適用方法

### A. 自動パッチ（ローカル）
```bash
# app.py と同じディレクトリに v314_recommendation.py を置く
python3 apply_v314_to_app.py app.py
# アプリ再起動
```

### B. 手動
1. `v314_recommendation.py` を `app.py` と同じ場所へ
2. `app.py` 先頭付近:
   ```python
   from v314_recommendation import v314_live_recommendation
   ```
3. ファイル末尾（または推奨関数定義の後）:
   ```python
   _v305_live_recommendation = v314_live_recommendation  # Ver314
   ```
4. 表示バージョンを `Ver314` に変更

### C. 再シミュレーション
バージョンフィルタで **Ver314** を選び、同じ期間で推奨回収率を確認。

## 閾値の調整
`v314_recommendation.py` 先頭:
```python
V314_EV_MIN = 1.0
V314_EV_MAX = 2.0
V314_MAX_TICKETS = 10
```
EV上限を 2.2 に上げると推奨率がやや増える（回収は下がる可能性）。

## ファイル
- `v314_recommendation.py` … 本体
- `apply_v314_to_app.py` … app.py への一括適用
- `VER314_RECOMMENDATION.md` … 本説明
