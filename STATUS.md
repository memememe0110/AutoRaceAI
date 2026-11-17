# AutoRaceAI 進捗

更新: 2026-11-17

通常予測は停止中。main への直接 push はしない。修正はブランチ + PR。

## 進行中

| 項目 | 状態 | 場所 |
|------|------|------|
| 選手履歴の強制再取得 | テスト補強済み・未マージ | [PR #26](https://github.com/memememe0110/AutoRaceAI/pull/26) |
| 前向き収集の APP_COMMIT 固定 | 未マージ | [PR #25](https://github.com/memememe0110/AutoRaceAI/pull/25) |
| 8周レースの周回数延長 | 調査済み・実装待ち。実周回数対応、終盤補正、出力比較まで必要 | `fix/lap-count-audit` |
| worker 実行中の UI rerun による global 上書き | 原因調査未完了。特に `_V330A_EXPERIMENT` の上書きと worker 終了時の復元漏れ | 調査継続 |
| 実DBの全レース件数・登録完全性監査 | 過去のDB世代だけでは最新状態を確定できていない。全レース件数と部分登録を確認 | 読み取り専用監査 |
| Ver331 Lap1精度改善 | 測定前提の整合性確認から再開。補正ロジックは未投入 | `_V331_LAP1_TUNE=False` |

## 次にやること

- **Lap1予測精度の基準測定をやり直す（Ver330-A基準）**
  - 以前の377R評価は225/377で停止した暫定値。確定評価として扱わない
  - race_key集合、対象377R、seed、trials、DB SHA、保存バージョン、集計方法を突合し、過去の測定値との乖離原因を特定
  - Lap1全分布を保存する。通常の `v252_lap_prediction_snapshots` はmodal隊列のみなので、validation時の全分布保存を使う
  - Lap1専用指標として TOP1 / TOP3 / pattern_rank / relative_lift を集計
  - 初期状態・初期gap式（`0.11 + 0.017 * handicap差 + uniform(0, 0.08)`）を監査
  - まず測定し、補正値はデータが選ぶ。補正候補の分類とA/B比較は基準値確定後
  - `ver331-lap1.yml` を整備し、同一 `raw_text / trials / seed` でVer330-A対Ver331を比較
  - Lap1変更はLap2〜6へ伝播するため、副作用を周回別CSVで監視する
  - 変更対象はLap1初期gap式のみ。start_score・Lap2〜6ロジックは変更しない。bit-identicalは要求せず、後続周回への影響を測る
  - `_V331_LAP1_TUNE=False` を通常状態として維持し、検証時のみ有効化
- **Ver330-A / Ver326 の実保存版切替調査**
  - `v231_prediction_history` と `v187_mixed_plan_runs` の実保存版・切替時刻を確認
  - redeploy、session timeout、UI rerun、global上書きの時系列を分けて検証
  - SQL-A/Bの実行結果が未取得。実DBの証拠なしに原因を確定しない
- **worker/global競合の調査**
  - `_v319_bg_import_worker` 実行中にStreamlit UI rerunが `_V330A_EXPERIMENT` を上書きする可能性を確認
  - worker終了時の finally で元のglobal設定が復元されるか確認
- **予測・結果の欠損監査**（実DBの件数突き合わせ。予測の存在、`result_races` / `result_entries` / `result_laps` / `result_payouts`、予測周回数と実周回数。画面表示上の欠損もDB実体と切り分ける）
- **全レース件数監査の完了**（2026-01-01以降を対象に、開催レース数と登録件数を照合。部分登録・未登録レースを抽出。以前の世代693/701/705等の監査結果を最新DBの確定値として扱わない）
- 山陽10R（2026-11-14）のハンデ再収集。PR #24 反映後、ログで `handicap_source_missing` / `handicap_parse_failed` を確認してから。実データで 0m, 10m, 10m... が復元されることを確認
- 既存の全員0mスナップショットの再生成。再生成後、ハンデ分析の基準データを更新
- 結果登録時の `result_entries` の `race_time` / `start_time` 欠損と、`OtherRaceInfo` の結合・欠損判定を監査
- 周回ラベル変換・パースの不一致を監査（公式の「N周」「N周目」表記、ゴール線を含む周回の対応）
- 登録済み判定が「レース行の存在」だけで完了扱いになっていないか、関連テーブルの全数チェックで確認
- 701件の `parser_bug` 分類は、登録時刻のタイムゾーンと締切前後を照合して確定。根拠不足なら未確定のままにする
- 天候欠損は unknown として扱い、欠損を晴れ等へ補完していないことを監査
- 必要なら `history_only` 経路へ `force_history_refresh` を伝播（現状の PR #26 の対象は通常の公式取込経路）
- 必要性を再確認したうえで、旧DB世代693のコピー上で Worker / qver_lock8 の再現検証を実施（本番DB・mainを変更しない）
- PR #24反映後の前向き収集でハンデ修正が実際に使われていることを確認する。PR #25のAPP_COMMIT pinは未マージなので、旧SHAを使った収集ログを修正反映済みの証拠にしない

## 8周の調査結果

- 着順確率の `simulate()` は周回ループを持たない別経路。6周固定で誤っているわけではない。
- 周回隊列の `_v230_six_lap_simulation` は `for lap in range(1, 7)`。`meta["周回数"]` を見ない。
- ループを伸ばすだけだと 7・8周は隊列更新されるが、終盤ルール（late_release、Ver330-A抑制）は 5・6周固定のまま。
- 推奨: 実周回数まで延長し、7・8周は6周と同じ終盤ルール。6周レースの出力は変えない。
- 実装時はペア事前計算、late_release キャッシュ、監査辞書、周回別出力・比較・fingerprint も実周回数に対応させる。6周レースの既存出力が同一であることを回帰テストする。

## 完了

- PR #24 ハンデパーサ修正（main マージ済み）。欠損を 0m にしない。
- キュー版固定（登録時に予測版・seed・config を固定）。
- 8周の原因特定。

## 運用メモ

- main 以外は修正ブランチを作って push してよい。
- 予測に関わる修正は通常予測停止中なので進めてよい。
- PR #25 / #26 は未マージ。マージは明示承認後。
