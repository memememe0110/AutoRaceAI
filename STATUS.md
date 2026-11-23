# STATUS.md

最終更新: 2026-11-23 (JST)
対象: Issue #31「予測データの再現性・保存整合性を監査する」
監査ブランチ: `tmp-audit-31-repeats`

> このファイルは、Issue #31 の監査経過を引き継ぐための記録。監査専用ブランチ上に新規作成した。既存の `STATUS.md` は監査ブランチおよび main のツリーに存在しなかったため、今回ルートに追加した。Issue #31 本体、予測ロジック、本番DBは変更していない。

## 1. 現在の結論

重複保存された109件の再実行元について、全件の参照IDとバッチ再実行フラグを静的pickle opcode解析で確認した。再実行元レコードは次の内訳で確認できた。

- 72件: DB世代705で参照先を直接発見し、子レコードと `race_key`、`settings_hash`、`seed`、`trials`、`raw_text` SHA-256 が一致。
- 37件: DB世代705では見つからなかったが、過去DB世代489および490の両方で参照先を発見。37件すべてで `race_key`、`seed`、`trials`、`raw_text` SHA-256 が一致。

よって109件すべてについて、参照先となる再実行元履歴の実在を、世代705または過去世代のDBで確認できた。37件の参照元は世代489・490の時点では存在し、世代705では不在である。

**未確定:** 元履歴37件が消えた正確な時点・操作・原因。Job 368〜370と個別レース履歴の直接対応。時間窓の一致は相関であり、直接JOINではない。

## 2. 過去DB世代の監査結果

### Generation 489

- DBコミット: `1591b29cce19cfcba38bd32238c1e8c3100246f0`
- 保存時刻: 2026-10-15 09:50 JST
- Run: [#45046035618](https://github.com/memememe0110/AutoRaceAI/actions/runs/45046035618)
- 3ジョブ: `test`、`audit`、`historical-root-audit` がすべてPASS
- 外部元履歴: 37/37件を発見
- 一致: `race_key` 37/37、`seed` 37/37、`trials` 37/37、`raw_text` SHA-256 37/37
- 不一致: `app_version` 0/37、`settings_hash` 0/37
- 元履歴側は全件 `Ver323`、子レコード側は全件 `Ver326`。元履歴側 `settings_hash=13a3b266ac35fdad`、子側 `6a6084b95aa3262e`。
- DB integrity_check: `ok`
- DB open mode: read-only

### Generation 490

- DBコミット: `dafc1b1fda3ba69c809f8234b54b206c58dcfad7`
- 保存時刻: 2026-10-15 12:57:22 JST
- Run: [#45105715745](https://github.com/memememe0110/AutoRaceAI/actions/runs/45105715745)
- 3ジョブ: `test`、`audit`、`historical-root-audit` がすべてPASS
- 外部元履歴: 37/37件を発見
- 一致: `race_key` 37/37、`seed` 37/37、`trials` 37/37、`raw_text` SHA-256 37/37
- 不一致: `app_version` 0/37、`settings_hash` 0/37
- 元履歴側は全件 `Ver323`、元履歴側 `settings_hash=13a3b266ac35fdad`。子レコード側は全件 `Ver326`、`settings_hash=6a6084b95aa3262e`。
- DB integrity_check: `ok`
- DB open mode: read-only

### 消失時点について

世代489 (10/15 09:50 JST) と世代490 (10/15 12:57 JST) の両方で37件すべてが存在し、世代705では37件すべてが見つからない。したがって、確認できたスナップショット間では、元履歴37件の不在化は **世代490の保存後から世代705の保存時点まで** に発生した。

この範囲内の正確な時刻、DB世代、削除・置換・再構築等の操作原因は未特定。世代490の保存時刻はJob 368作成時刻 (10/15 12:30 JST) より後であるため、Job 368実行中またはその後の状態を含むスナップショットである。ただし、この時間関係だけで個別レースとJob 368を直接結びつけることはできない。

## 3. 重複保存と再実行の証拠

- Run #10 の安全な静的解析で、重複48グループ188行について `rerun_source_history_id` のopcode隣接値を188/188件抽出。`batch_rerun=True` も188/188件。
- 厳密なVer326同一条件37グループ109行についても、109/109件で `batch_rerun=True`。
- 109件の参照関係は、37レースごとの再実行チェーンを構成。72本のチェーン内参照と37本の外部root参照。
- 72件の内部参照は世代705で実在と同一入力を直接照合済み。
- 37件のroot参照は世代489・490で実在と同一入力を照合済み。

### force_current と Job 368〜370

Job 368〜370はすべて `batch_rerun`、`app_version=Ver326`、`force_current=1` のメタデータを持つ。既存の `_v231_save_prediction_history()` はinsert-onlyで重複防止を行わず、`force_current=True` は現行バージョンの既存行も再処理できる。

このため、**force_current=1 を指定した複数バッチ再実行が、109件の重複保存を生んだ最有力候補**である。ただし、Job 368〜370の各ジョブと個々のrace_keyを結ぶ永続的な直接対応は確認できていない。Jobの時間窓と履歴時刻の近接は相関であり、直接的な因果証明ではない。

Jobメタデータ:
- Job 368: completed、40/40、作成 2026-10-15 12:30:27 JST、終了 21:04:47 JST
- Job 369: failed (stale job auto-reaped)、120/200、作成 2026-10-15 21:13:45 JST、終了 2026-10-16 00:15:17 JST
- Job 370: failed (stale job auto-reaped)、40/100、作成 2026-10-16 00:17:34 JST、終了 09:07:11 JST
- Job 369/370の時間窓候補にhistory_id 6849の共有があるが、これも直接ジョブ紐付けではない。
- `processed_race_keys` はworker内ローカルで永続化されない。stale job reaperは進捗停止20分でfailed化し、自動再起動はしない。`_v290_recover_orphaned_batch_job` はmanual_controlで自動復旧無効。

## 4. 監査実行履歴・Workflow修正

- Run #45046035618: 世代489監査。3ジョブPASS。
- Run #45105645521: 世代490用Workflowの初回実行。shellの行継続記号を誤って二重化したため、DB取得コマンドの引数が正しく渡らず失敗。DBや監査ロジックの失敗ではない。
- 修正コミット: `bae52df13f1350ff0c6959b1e312f4c33152a928`。shell行継続記号を修正し、歴史rootテストもテストジョブで実行するようにした。
- Run #45105715745: 修正版Workflow。テスト2種、世代705再監査、世代490外部元照合の3ジョブすべてPASS。
- 先行する単独Workflow Run #45045988172 はジョブ0件で有効な監査結果を生成していない。後続では既存監査Workflowに歴史root監査jobを統合した。

## 5. 安全性・変更範囲

- DB取得・照合はSQLite read-only mode (`mode=ro`) と `PRAGMA query_only=ON` を使用。
- DB integrity_checkは対象スナップショットで `ok`。
- pickleは `pickletools.genops` による静的opcode解析のみ。`pickle.loads` / `pickle.load` は未使用。
- 予測ロジック、本番コード、予測結果、DBデータは変更していない。
- 変更は監査ブランチ上の監査スクリプト、テスト、Workflow、およびこの引き継ぎ用STATUS.mdに限定。
- Issue #31本文や他Issue (#28/#29) は変更していない。

## 6. 次の調査

1. 世代490以降、世代705以前のDBスナップショットを追加調査し、37件の消失境界を狭める。
2. DB保存履歴・manifest・コミット履歴から、該当履歴行が見えなくなった最初の世代を特定する。
3. Job 368〜370とrace_keyの直接対応を示す永続データがないか、別テーブル・workerログ・結果blobの静的調査を継続する。pickle payloadの実行・デシリアライズは禁止。
4. 直接対応が見つからない限り、Jobとの関係は「最有力候補・相関」に留める。
5. 十分な証拠が揃った後、ユーザー承認を得てIssue #31と正式なステータス文書へ反映する。
