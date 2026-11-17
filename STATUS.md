# AutoRaceAI 進捗ボード

更新: 2026-11-17

通常予測は停止中。main への直接 push はしない。修正はブランチ + PR。マージは明示承認後。

分類: **予測** = 予測ロジック・精度・保存版に効く / **データ** = 取込・登録・監査 / **運用** = デプロイ・Workflow・競合

進捗: 未着手 / 進行中 / レビュー待ち / 完了

管理の分け:
- このファイル: 全体の優先度と進捗
- Issues: 個別タスクの実行・検証・完了
- PR: コード変更のレビューとマージ

## 最優先

| 進捗 | 分類 | Issue | タスク | 現状 | 次 |
|------|------|-------|--------|------|----|
| 進行中 | 予測 | [#28](https://github.com/memememe0110/AutoRaceAI/issues/28) | Ver331 Lap1精度改善 | 測定前提が未整合。225/377は暫定。補正は未投入。`_V331_LAP1_TUNE=False` | race_key集合・seed・trials・DB SHA・保存版・集計方法を突合してから基準測定 |
| 進行中 | 予測 | [#29](https://github.com/memememe0110/AutoRaceAI/issues/29) | Ver330-A / Ver326 の実保存版切替調査 | `v231_prediction_history` / `v187_mixed_plan_runs` の実保存版と切替時刻が未確認 | SQLで実保存版と切替時刻を出す。証拠なしに原因を確定しない |
| レビュー待ち | 予測 | [#30](https://github.com/memememe0110/AutoRaceAI/issues/30) | 8周シミュレーション対応 | [PR #27](https://github.com/memememe0110/AutoRaceAI/pull/27) 未マージ。検証モード20,000試行は完走。6周対照も一致。マージ判断待ち | マージ判断。30m以上の終盤再加速は合成入力で別途確認 |
| 進行中 | 運用 | [#32](https://github.com/memememe0110/AutoRaceAI/issues/32) | worker中のUI rerunによるglobal上書き | `_V330A_EXPERIMENT` の上書きと、worker終了時の復元漏れが未解決 | worker実行中の上書き有無と finally 復元を確認 |
| 進行中 | データ | [#33](https://github.com/memememe0110/AutoRaceAI/issues/33) | 実DBの登録完全性監査 | #35の細部監査を統合済み。#17は周回履歴の将来予測影響で別目的 | 2026-01-01以降の開催件数と登録件数を照合。部分登録を抽出 |

## 高

| 進捗 | 分類 | Issue | タスク | 現状 | 次 |
|------|------|-------|--------|------|----|
| レビュー待ち | 運用 | [#34](https://github.com/memememe0110/AutoRaceAI/issues/34) | 前向き収集のAPP_COMMIT固定 | [PR #25](https://github.com/memememe0110/AutoRaceAI/pull/25) 未マージ。旧SHAの収集は修正反映の証拠にしない | マージ後に1R以上収集し、ハンデ欠損カウンターを確認 |
| 未着手 | 予測 | [#31](https://github.com/memememe0110/AutoRaceAI/issues/31) | 予測データの再現性・保存整合性 | 同一レースのseed・試行回数・DB世代・保存版の突合が未完了 | 突合条件を固定して差分を出す |

## 中

| 進捗 | 分類 | Issue | タスク | 現状 | 次 |
|------|------|-------|--------|------|----|
| レビュー待ち | データ | [#26](https://github.com/memememe0110/AutoRaceAI/pull/26) | 選手履歴の強制再取得 | PR #26 通常の公式取込経路のみ。未マージ | レビュー後マージ。history_onlyは [#37](https://github.com/memememe0110/AutoRaceAI/issues/37) |
| 未着手 | データ | [#36](https://github.com/memememe0110/AutoRaceAI/issues/36) | 旧ハンデ0mデータの再生成 | PR #24はmain済み。PR #25未マージ。再収集前 | 新コード確認後に再収集。0,10,10,10,10,10,10,10 を確認 |
| 未着手 | 運用 | [#37](https://github.com/memememe0110/AutoRaceAI/issues/37) | history_only への強制履歴更新 | PR #26の対象外 | 必要かを判断してから別PR |
| 未着手 | データ | [#17](https://github.com/memememe0110/AutoRaceAI/issues/17) | 欠損周回履歴の将来予測影響 | #33とは別目的。対象12Rの履歴影響 | 隔離DBで影響を定量化 |
| 未着手 | データ | [#14](https://github.com/memememe0110/AutoRaceAI/issues/14) | `_v35_parse_entries` 二重定義 | デッドコード整理 | 影響確認後に削除または警告 |
| 未着手 | データ | [#22](https://github.com/memememe0110/AutoRaceAI/issues/22) | Brier本評価前の4修正採用判断 | 試運転用。本評価前に判断 | 40R本評価前に採否を決める |

## 完了

| 分類 | タスク | 結果 |
|------|--------|------|
| データ | ハンデパーサ修正 | [PR #24](https://github.com/memememe0110/AutoRaceAI/pull/24) mainマージ済み。欠損を0mにしない |
| 予測 | キュー版固定 | 登録時に予測版・seed・configを固定 |
| 予測 | 8周の原因特定 | `_v230_six_lap_simulation` が1-6固定。`simulate()` は別経路 |

## #30 の証跡

- 検証モード・浜松12R・20,000試行: KeyErrorなしで完走。
- 1〜8周の指紋とgapが保存され、6周の単純コピーではない。
- Ver330-A抑制は6周2938、7周5471、8周8184回発火。
- このレースは0m/10mなので終盤再加速のeligibleは0。不合格理由にはしない。
- 飯塚2R、同一入力・seed・2000試行でmainとPRの代表隊列と着順期待値が一致。
- PR #27は未マージ。

## Ver331 Lap1 の制約

- 変更対象は Lap1 の初期gap式のみ。start_score、Lap2〜6のロジックは変えない。
- `_V331_LAP1_TUNE=False` を維持し、検証時だけ有効化。
- 同一 raw_text / trials / seed で比較する。
- Lap1だけ変えて Lap2〜6 を完全一致させることはできない。後続周回への影響は別途測る。
- 補正値は先に入れない。基準測定の不一致を解消してから。

## 運用メモ

- main 以外は修正ブランチを作って push してよい。
- 予測に関わる修正は通常予測停止中なので進めてよい。
- PR #25 / #26 / #27 は未マージ。
- 別の人が取る場合は、対象Issueにコメントしてからブランチを切る。同じIssueを二人で同時に触かない。
