# AutoRaceAI 進捗

更新: 2026-11-17

通常予測は停止中。main への直接 push はしない。修正はブランチ + PR。

## 進行中

| 項目 | 状態 | 場所 |
|------|------|------|
| 選手履歴の強制再取得 | テスト補強済み・未マージ | [PR #26](https://github.com/memememe0110/AutoRaceAI/pull/26) |
| 前向き収集の APP_COMMIT 固定 | 未マージ | [PR #25](https://github.com/memememe0110/AutoRaceAI/pull/25) |
| 8周レースの周回数延長 | 調査済み・実装待ち | `fix/lap-count-audit` |

## 次にやること

- 予測・結果の欠損監査（実DBの件数突き合わせ。予測の存在、result_races / entries / laps / payouts、予測周回数と実周回数）
- 山陽10R（2026-11-14）のハンデ再収集。PR #24 反映後、ログで `hand_source_missing` / `hand_parse_failed` を確認してから
- 既存の全員0mスナップショットの再生成
- 必要なら `history_only` 経路へ `force_history_refresh` を伝播

## 8周の調査結果

- 着順確率の `simulate()` は周回ループを持たない別経路。6周固定で誤っているわけではない。
- 周回隊列の `_v230_six_lap_simulation` は `for lap in range(1, 7)`。`meta["周回数"]` を見ない。
- ループを伸ばすだけだと 7・8周は隊列更新されるが、終盤ルール（late_release、Ver330-A抑制）は 5・6周固定のまま。
- 推奨: 実周回数まで延長し、7・8周は6周と同じ終盤ルール。6周レースの出力は変えない。

## 完了

- PR #24 ハンデパーサ修正（main マージ済み）。欠損を 0m にしない。
- キュー版固定（登録時に予測版・seed・config を固定）。
- 8周の原因特定。

## 運用メモ

- main 以外は修正ブランチを作って push してよい。
- 予測に関わる修正は通常予測停止中なので進めてよい。
