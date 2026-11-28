# AutoRaceAI 進捗ボード

> 正式進捗正本。リポジトリ内パス: `docs/status/STATUS.md`。正本ブランチ: `docs/status`。

更新: 2026-11-21

通常予測は停止中。main への直接 push はしない。修正はブランチ + PR。

分類: **予測** = 予測ロジック・精度・保存版に効く / **データ** = 取込・登録・監査 / **運用** = デプロイ・Workflow・競合

進捗: 未着手 / 進行中 / レビュー待ち / 完了

管理の分け:
- このファイル: 全体の優先度と進捗
- Issues: 個別タスクの実行・検証・完了
- PR: コード変更のレビューとマージ

マージ方針:
- コードレビューOK、必要なテストPASSなら都度確認せずマージする。
- 実データE2Eが必要な変更は、その検証もPASSしてからマージする。
- 未解決の不具合やデータ破壊リスクがあれば、マージせず理由を報告する。
- mainへの直接pushはせず、PR経由を維持する。

## 最優先

| 進捗 | 分類 | Issue | タスク | 現状 | 次 |
|------|------|-------|--------|------|----|
| 進行中 | 予測 | [#28](https://github.com/memememe0110/AutoRaceAI/issues/28) | Ver331 Lap1精度改善 | 測定モードとSHA固定は [PR #38](https://github.com/memememe0110/AutoRaceAI/pull/38) でmain済み。Ver326の2R×2回は分布一致。377R基準は実行中 | 基準測定完了後に不一致を解消。補正は未投入 |
| 進行中 | 予測 | [#29](https://github.com/memememe0110/AutoRaceAI/issues/29) | Ver330-A / Ver326 の実保存版切替調査 | [PR #40](https://github.com/memememe0110/AutoRaceAI/pull/40) mainマージ済み。generation 705でhistory/payload 0不一致、queue 128行0不一致。1,437レース/540複数版。restore孤立55、pred-time snapshot孤立50,230行、lap版のみ318組、結果履歴なし189R | 孤立データが旧世代の保持仕様か欠損か分類。削除・上書きはしない |
| 完了 | 運用 | [#32](https://github.com/memememe0110/AutoRaceAI/issues/32) | Worker global上書き競合 | [PR #39](https://github.com/memememe0110/AutoRaceAI/pull/39) mainマージ済み。Worker設定/校正基準をthread-local化。Ver326/327/328/330-Aの実験フラグもsnapshot優先。専用テストとqueue所有権/並行回帰PASS | 完了 |
| 進行中 | データ | [#33](https://github.com/memememe0110/AutoRaceAI/issues/33) | 実DBの登録完全性監査 | 第1段階のresult_racesメタデータ保存は [PR #46](https://github.com/memememe0110/AutoRaceAI/pull/46) でmainへマージ済み（merge commit `21957f7`）。旧DB不足列追加・賞金NULL・NULL再登録時の8項目保持を検証。賞金123456円を初回保存し、全項目NULLで再登録しても既存値が保持される回帰テストは一時Run #3 SUCCESS、PR headでもpytest check SUCCESS。Issue #33は継続 | 次はresult_entries（所属・人気・事故情報）を別PRで実装。その後result_laps/result_payouts/監査Workflowへ進む |

## 高

| 進捗 | 分類 | Issue | タスク | 現状 | 次 |
|------|------|-------|--------|------|----|
| 進行中 | 予測 | [#31](https://github.com/memememe0110/AutoRaceAI/issues/31) | 予測データの再現性・保存整合性 | Run #8で実DB generation 705の重複監査完了。3228行/540重複race/2331重複行。実DB12列・NOT NULL制約確認、DB SHA不変、integrity_check=ok。Artifact ID 14642433960 | 48組188行を時系列再分析。Ver326/20,000の同一settings_hash・seed・trials・input SHAの完全一致fingerprintが37組で複数回保存（35組×3回、2組×2回、計109行）。10/15〜10/16の複数波で、隣接間隔最短11,036秒/中央値31,500秒。60秒未満・1時間未満0。Ver327は10/22〜23、Ver328は10/26に版別波。大規模9組559行も96完全fingerprint群中80群で反復（543行）。Ver326/327/328の版別バッチ波と重複が混在。短周期ポーリングは否定的だが、Ver326同一条件の複数回実行経路は未特定。Ver326ソースsnapshot（10/21 commit b13a319f）では_v231_save_prediction_historyは毎回INSERTし、重複検索なし。_v326_find_matching_prediction_history / fingerprint helperも未存在。保存呼出元は通常foreground、単発BG worker、_v262_batch_rerun_saved_histories、_v319_run_prerace_prediction等。batchはforce_current=Trueで現行版も再実行可能。_v319_run_prerace_predictionには履歴INSERT後の復元保存失敗時にslim payloadで再INSERTし得るfallbackあり。ただし観測間隔最短11,036秒のため同一処理内二重保存だけでは説明困難。重要な制約：10/15 app.py snapshot（e377076/c0a0356）は_V231_APP_VERSION=Ver324で、10/15〜16のDB Ver326行と不一致。10/21 Ver326 snapshotを10/15の実稼働版と断定できない。batch rerunが最優先候補：_v278_bg_startはforce_current=Trueで現行版保存済みraceも対象にでき、_v278_bg_workerはprocessed_race_keysで同一job内の重複だけを防ぐ（別jobには除外集合を引き継がない）。複数jobが同一raceを再保存でき、観測された数時間〜数日間隔と構造上整合。ただしjob単位の証拠なし。既存duplicates.csvにpayload/job detailなし、version-integrity auditもjob_type/status/app_version/save_version集計のみ。次は対象期間のv278_background_jobs個別行（job_id/force_current/created_at/result_blob）とprocessed_race_keys、history payloadのbackground_prediction/batch_rerun/walk_forward_v269/_v301_bg_job_idを37群と照合。10/15〜16の実稼働版不一致も未解決。batch rerun最有力・原因未確定。DB読み取り専用、コード/Workflow/保存関数変更なし。Run #8: 9 tests PASS / audit SUCCESS。#28隔離、#29変更なし |

### 追加確認（2026-11-22）
- Run #8（44764670690）のartifactは `duplicates.csv` と `summary.json` の2ファイルで、個別job行/history payloadは含まれない。
- **補足訂正：既存のPrediction Version Integrity Audit成果物が別に存在した。** Run `44117299639`（generation 705、artifact ID `14344016653`）を取得し、`prediction_version_table_inventory.csv` / summaryのtable_inventoryを確認。これはDB上の `PRAGMA table_info` から列名を抽出した既存出力。
- `v278_background_jobs` の実DB列名は `app_version,cancel_requested,created_at,current_label,done_count,error_text,finished_at,force_current,job_id,job_type,limit_count,message,odds_only,pause_requested,result_blob,save_version,started_at,status,total_count,updated_at` の20列。**race_key / task_id / batch_id / run_id は存在しない。** したがって `j.race_key=h.race_key` の直接JOIN案は不可。result_blob内の `processed_race_keys` を抽出してhistoryのrace_keyと照合する方式が必要。
- 既存artifactは列名のみを保存し、PRAGMAのtype/notnull/dflt_value/pkは出力していない。完全なPRAGMA行は未取得。
- 現行audit scriptは背景jobをjob_type/status/app_version/save_version単位で集計するだけで、個別job行・result_blobを出力しない。個別照合には読み取り専用の追加出力が必要。workflow_dispatch専用アクションは利用可能ツールにないため、Workflow起動は行っていない。
- 48グループは母集団、37グループはそのうちVer326内のsettings_hash/seed/trials/input SHA/app_versionが完全一致して複数回保存された重点対象。残り11グループはこの厳密条件に該当しない。
- 判定は「batch rerunが最有力候補、原因未確定」。DB読み取り専用、保存関数変更なし、#28/#29変更なし。

## 中

| 進捗 | 分類 | Issue | タスク | 現状 | 次 |
|------|------|-------|--------|------|----|
| 未着手 | データ | [#36](https://github.com/memememe0110/AutoRaceAI/issues/36) | 旧ハンデ0mデータの再生成 | PR #24と#25はmain済み。再収集前 | 新コード確認後に再収集。0,10,10,10,10,10,10,10 を確認 |
| 未着手 | 運用 | [#37](https://github.com/memememe0110/AutoRaceAI/issues/37) | history_only への強制履歴更新 | PR #26は通常経路としてマージ済み。history_onlyは未対応 | 必要かを判断してから別PR |
| 未着手 | データ | [#17](https://github.com/memememe0110/AutoRaceAI/issues/17) | 欠損周回履歴の将来予測影響 | #33とは別目的。対象12Rの履歴影響 | 隔離DBで影響を定量化 |
| 未着手 | データ | [#14](https://github.com/memememe0110/AutoRaceAI/issues/14) | `_v35_parse_entries` 二重定義 | デッドコード整理 | 影響確認後に削除または警告 |
| 未着手 | データ | [#22](https://github.com/memememe0110/AutoRaceAI/issues/22) | Brier本評価前の4修正採用判断 | 試運転用。本評価前に判断 | 40R本評価前に採否を決める |

## 完了

| 分類 | Issue | タスク | 結果 |
|------|-------|--------|------|
| 予測 | [#30](https://github.com/memememe0110/AutoRaceAI/issues/30) | 実周回数対応 | [PR #27](https://github.com/memememe0110/AutoRaceAI/pull/27) mainマージ済み。6/7/8周に応じて展開。6周対照一致。検証モード20,000試行完走 |
| 運用 | [#34](https://github.com/memememe0110/AutoRaceAI/issues/34) | 前向き収集のAPP_COMMIT固定 | [PR #25](https://github.com/memememe0110/AutoRaceAI/pull/25) mainマージ済み |
| データ | - | 選手履歴の強制再取得 | [PR #26](https://github.com/memememe0110/AutoRaceAI/pull/26) mainマージ済み。通常の公式取込経路。history_onlyは#37 |
| データ | - | ハンデパーサ修正 | [PR #24](https://github.com/memememe0110/AutoRaceAI/pull/24) mainマージ済み。欠損を0mにしない |
| 予測 | - | キュー版固定 | 登録時に予測版・seed・configを固定 |
| 予測 | - | 8周の原因特定 | `_v230_six_lap_simulation` が1-6固定。`simulate()` は別経路 |


## 直近の更新（2026-11-19）

| 分類 | Issue / PR | 状態 | 確認結果・次 |
|------|------------|------|--------------|
| データ | [PR #46](https://github.com/memememe0110/AutoRaceAI/pull/46) / [Issue #33](https://github.com/memememe0110/AutoRaceAI/issues/33) | PR完了・Issue継続 | merge commit `21957f7`。result_racesメタデータ保存、旧DB不足列追加、NULL賞金、NULL再登録時の8項目保持テストをmainへ反映。Run #3とPR headのpytest checkはSUCCESS。#28測定環境は変更なし | Issue #33の次段階としてresult_entriesを別PR化 |
| 運用 | [PR #47](https://github.com/memememe0110/AutoRaceAI/pull/47) | 完了 | input SHA必須化をmainへマージ（merge commit `122a8bc`）。29R Brier再測定Run #14はSUCCESS、28/29R評価、BS_macro=0.3235148327。除外1Rはcar_set不一致。今回の再測定では既知基準値から変化なし。Issue #28のLap1基準測定完了とは別 |

| データ | [Issue #31](https://github.com/memememe0110/AutoRaceAI/issues/31) | Run #8 完了 | レビュー済み2ファイルを `tmp-audit-31-duplicates` に反映。Run #7は先行した1ファイルだけの中間コミットでtest失敗、Run #8は両ファイル揃ったhead `8c509077584a4343da854e41a9801a3cf053f4fa` でtest/auditともSUCCESS。9 tests / TEST_COUNT_GATE_PASS: 9。generation 705、DB SHA `57a6f7d4e8db23e9560e66ffbc2130f3cf1156164b3d760513c3da33f7535e67`、integrity ok。3228 history rows、540 duplicate races、2331 duplicate rows。classification_group_counts: exact-input 48 / input-diff 121 / execution-conditions-diff 371。raw_text欠損0。`duplicates.csv` / `summary.json` Artifact ID `14642433960`。DB変更検出なし（SHA不変） | 48件の同一条件・同一入力候補を精査。Issue #31は継続 |

## Ver331 Lap1 の制約

- 変更対象は Lap1 の初期gap式のみ。start_score、Lap2〜6のロジックは変えない。
- `_V331_LAP1_TUNE=False` を維持し、検証時だけ有効化。
- 同一 raw_text / trials / seed で比較する。
- Lap1だけ変えて Lap2〜6 を完全一致させることはできない。後続周回への影響は別途測る。
- 補正値は先に入れない。基準測定の不一致を解消してから。

## 運用メモ

- main 以外は修正ブランチを作って push してよい。
- 予測に関わる修正は通常予測停止中なので進めてよい。
- 別の人が取る場合は、対象Issueにコメントしてからブランチを切る。同じIssueを二人で同時に触かない。

## Issue #31 正式監査到達点（2026-11-24）

**正式判定：主要監査完了・再発防止未完了**

### Gen675→Gen676の37件消失

- Gen675では対象37件が37/37存在し、Gen676では0/37。消失境界はGen675→Gen676。
- 対象はVer322〜324。世代間差分は `v231_prediction_history` が1,365行減、`v252_lap_prediction_snapshots` が8,196行減。
  - 履歴: Ver322 447行、Ver323 447行、Ver324 471行。
  - Lap snapshot: Ver322 2,682行、Ver323 2,682行、Ver324 2,832行。
- 旧Ver整理処理を隔離SQLiteで再現し、Gen676相当の状態を行単位ハッシュまで照合。技術的再現は確立したが、本番での実操作を証明したものではない。
- Gen675由来の回収Artifactによる回収・検証は確認済み。元DBの直接変更・上書きはしていない。
- これはVer326の同一fingerprint反復保存109件（37 race_key、force_current=1を含む再実行調査）とは別事象。両者の因果関係は主張しない。

### 未証明事項

- 実際の操作イベントと実行操作者
- UIで指定された `keep_versions`
- 専用アーカイブファイルが作成・保持されていたか

したがって、旧Ver整理処理による技術的再現は成立したが、2026-11-09前後の本番操作イベントは未証明。

### 再発防止設計の成果物

監査ブランチ `tmp-audit-31-repeats` に次の3文書を作成・push済み。

1. [現行アーカイブ処理仕様](https://github.com/memememe0110/AutoRaceAI/blob/tmp-audit-31-repeats/docs/audit/issue31-current-archive-spec.md)
2. [安全な整理処理の設計](https://github.com/memememe0110/AutoRaceAI/blob/tmp-audit-31-repeats/docs/audit/issue31-safe-cleanup-design.md)
3. [クリーンアップ試験計画](https://github.com/memememe0110/AutoRaceAI/blob/tmp-audit-31-repeats/docs/audit/issue31-cleanup-test-plan.md)

設計文書commit: `334c29a1af52ba1f7e17efe1cc786ec34316395e`  
監査ブランチ現HEAD: `c8f37551fe216c799b1e8a1858432c2673e2077a`

**状態：設計段階。実装・本番反映は未実施。** `app.py`、本番DB、予測ロジック、`APP_VERSION` は変更していない。設計レビュー完了前に実装・本番反映へ進まない。

### 次の作業

1. 3設計文書のレビューと指摘反映。
2. レビュー承認後、別途明示承認を得て隔離環境で実装・試験を検討。
3. 実操作イベント・操作者・`keep_versions`・専用アーカイブの証拠は、別途取得できた場合のみ確定する。

結論：Gen675→Gen676の37件消失について、消失境界・技術的再現・回収可能性の確認は完了。実際の操作イベントの証明と再発防止策の実装・検証は未完了。


## Issue #31 設計一次レビュー結果（2026-11-24）

**状態：主要監査完了・再発防止未完了。設計一次レビューは「要修正」、設計承認未了。**

監査ブランチ `tmp-audit-31-repeats` の3文書に一次レビュー指摘を追補する。今回の更新は設計文書と進捗記録のみであり、実装・本番反映は行わない。

### 必須修正 R1〜R6
- R1 SQLite識別/WAL: Backup API等による一貫snapshot、DB identity/schema/対象PK・row digest保存、削除直前再照合。WAL/同時更新/不一致時は削除禁止。
- R2 承認真正性: 認証済み操作者の明示承認、operation_id/plan_digest/一回限りnonce、有効期限・取消・再利用防止、対象・DB・計画変更時の承認失効。
- R3 外部ログ/commit: DELETE前の外部永続ログ、COMMIT_UNKNOWN、再起動後read-only照合、結果確定まで再DELETE禁止。
- R4 競合/原子性: 書き込みトランザクション内でPK集合・row digest・保護対象を再照合。2テーブルを同一トランザクションで処理し、不一致・片側失敗はrollback。
- R5 37 root: 通常整理から除外する本体保護対象。archive保存とは別要件。混入時は拒否。将来の削除は別設計と明示的追加承認が必要。
- R6 VACUUM: 削除安全性の証明から分離。失敗はPOST_DELETE_MAINTENANCE_FAILEDとして記録し、削除済みデータのarchive/restore/ログを保持。

### テスト計画
- T01〜T20を維持。T16をVACUUM失敗とデータ削除結果の分離テストに修正。
- T21 commit直後停止、T22外部ログ書込失敗、T23 WAL同時更新、T24 nonce再利用、T25 NULL/BLOB/数値/文字列digest、T26 schema/index/constraint変更、T27 operation_id並行実行、T28 37 root保護を追加。

### 成果物・Git状態
- 設計文書: `docs/audit/issue31-current-archive-spec.md`、`docs/audit/issue31-safe-cleanup-design.md`、`docs/audit/issue31-cleanup-test-plan.md`。
- 監査ブランチ: `tmp-audit-31-repeats`。今回の修正commit SHAは作成後に確定して追記する。
- 正式進捗正本ブランチ: `docs/status`。今回の更新commit SHAは作成後に確定して追記する。
- 次の作業: 3文書の再レビュー。R1〜R6とT21〜T28に未解決が1つでもあれば設計承認・実装開始に進まない。

### 変更禁止・未実施
- app.py、本番DB、予測ロジック、APP_VERSION、既存Artifactは変更していない。
- 実装・本番反映は未実施。実装開始には再レビュー完了後の明示承認が必要。

## Issue #31 設計レビュー修正Git記録（2026-11-24）

- 監査設計ブランチ: `tmp-audit-31-repeats`
- 監査設計文書・補助STATUS commit: `1d3210b22958c83db7acf8f8778924ad6171093c`
- 正式進捗正本ブランチ: `docs/status`
- 正式進捗正本の今回の修正commit: `53b00494839aba9fe7fa952fc3d9d2ed322b7ff3`
- Issue #31コメント: https://github.com/memememe0110/AutoRaceAI/issues/31#issuecomment-7133364016
- 次の作業: R1〜R6およびT21〜T28を含む3文書の再レビュー。未解決があれば設計承認・実装開始は禁止。
- 実装・本番反映は未実施。app.py、本番DB、予測ロジック、APP_VERSION、既存Artifactは未変更。

## Issue #31 設計再レビュー自己確認・次工程（2026-11-24）

**正式判定：主要監査完了・再発防止未完了。設計レビュー要修正。設計承認・実装承認は未了。**

監査ブランチ tmp-audit-31-repeats の3文書を再取得・修正し、文書上の自己レビューを実施した。

### 修正内容
- R1〜R4: SQLite Backup APIによる固定snapshotとWAL/metadata、source_db_identity、OIDC/MFA/nonce、DB内receiptとCOMMIT_UNKNOWN復旧表、BEGIN IMMEDIATE排他と処理順を規範仕様化。
- T16: POST_DELETE_MAINTENANCE_FAILEDへの一本化。重複差し替え節を削除。
- T21〜T25: 具体的fixture・期待値を追加。
- T01〜T28: 共通証跡schemaを追加。
- R5/R6、T22、T26〜T28: 前回判定を維持。T22/T26〜T28のテスト行は変更なし。
- current-archive-spec.mdは旧実装の監査事実と将来仕様を分離。

### 自己レビュー
3文書をGitHubから再取得。R1〜R4の未確定語句なし、T16重複なし、R5/R6・T22・T26〜T28の維持、T21〜T25 fixture、共通証跡schemaを確認した。

### 未実施・承認ゲート
SQLite/WAL実機競合、OIDC/MFA連携、fsync耐久、COMMIT_UNKNOWN強制停止の試験は未実施。独立再レビューと設計承認も未了。したがって正式状態は「設計レビュー要修正」を維持し、実装・テスト実装・PR・本番反映は禁止する。

### Git記録
- 監査ブランチ: tmp-audit-31-repeats
- 監査ブランチ最終HEAD: 9131f427377f8cbd05d7b7856c872d35aa6a640b
- R1〜R4/T21〜T28初回具体化: a9eae1d775e5a351842211ebdb25305738c52811
- 追加仕様commit: 8c7ab403716cc6f7cb17fc3de24687987d115578 / 2baa0c2d79346927b1e0642cf2ec7324c46b609c / 226dd2ba248f5bd9c873d06b2894b049bd22f574 / 50649df711259cb3abade8f9dcf5a2226e29ac7d / dfb5cf9b34033193b6eee2695cd6e4a6adc960a3 / 0b340b387306cb353ac5629c4428071980c9c3c9
- root STATUS自己レビュー・commit記録: 監査ブランチ最終HEADに含む
- 正式進捗更新commit: 作成後、追記commitで記録する
- Issue #31: 「再レビュー要修正」と未実施検証をコメントする

app.py、本番DB、予測ロジック、APP_VERSION、既存Artifactは変更していない。

### 正式進捗commit SHA追記（2026-11-24）
- Issue #31設計再レビュー状態の正式進捗更新commit: 46299e927736be1826443e4a20f8ca4331c1617d
- 上記commit SHAを記録した追記commit: 作成後にIssue #31コメントで最終HEADを報告する。

## Issue #31 独立設計再レビュー指摘と文書反映（2026-11-28 JST）

**正式状態：主要監査完了・再発防止未完了／設計レビュー要修正。設計承認・実装承認は未了。**

監査ブランチ `tmp-audit-31-repeats` の設計文書を独立観点で再確認し、6点の指摘をIssue #31へ記録した（comment: https://github.com/memememe0110/AutoRaceAI/issues/31#issuecomment-7239754361）。R3 receipt HMAC、R2 MFA鮮度/claim allowlist、R4 snapshot reader close順、R1 projection canonical order、T19/T23/T24/T25 fixture/coverageを文書へ反映した。

- 監査ブランチHEAD: `8c799f381f8bf14935e88931a533d67c2ce4c060`
- 設計書更新commit: `e7f04765187f6741cc4b9ccf2bc003e3a9fa70e4`, `14a14daf48d7606002fc2c11accbc6a72f67698b`, `8c799f381f8bf14935e88931a533d67c2ce4c060`
- テスト計画更新commit: `ff91ea5a1244108229d782b958907ad97b33a33e`, `240dd0f1ea86e0e3202b1d5e865f712eba6e572a`
- root STATUS追記commit: 作成後に監査ブランチHEADへ含めて記録する。

再取得による自己確認ではR5/R6、T16、T22、T26〜T28の維持を確認した。SQLite小規模挙動確認は実施したが、SQLite/WAL実機競合、OIDC/MFA、fsync耐久、COMMIT_UNKNOWN強制停止復旧は未実施。文書修正は設計承認・安全性実証を意味しない。実装・テスト実装・PR・本番反映は引き続き禁止する。app.py、本番DB、予測ロジック、APP_VERSION、既存Artifactは変更していない。

### receipt_digest canonicalization補正（2026-11-28 JST）
- 追加精査でreceipt_digestの入力にreceipt_digest自身が含まれる循環定義を発見し、receipt_digest自身とreceipt_hmacを除く全receipt列をSHA-256対象とするよう修正。receipt_hmacは計算済みdigestを含むreceipt record全体を認証する。
- 設計修正commit: 4561201a7a69641664f9b37b700a744438e9f572
- root STATUS追記commit: 9a5643135db7e922e033af8cdd4952998cbc645a
- 監査ブランチ最終HEAD: 9a5643135db7e922e033af8cdd4952998cbc645a
- 設計承認・実装承認は未了。SQLite/WAL、OIDC/MFA、fsync、COMMIT_UNKNOWN実機試験は未実施。実装・テスト実装・PR・本番反映は禁止を維持。

### canonical frame/receipt digestの最終補完（2026-11-28 JST）
- receipt_digestの自己参照を除外し、receipt_digest自身とreceipt_hmac以外のreceipt列をSHA-256対象とする定義へ修正。receipt_hmacは計算済みdigestとreceipt record全体を認証する。
- PROJv1/SCHEMAv1/PKv1のcanonical frameと件数・長さ表現を固定し、T25のTABLEv1/PKv1件数をu64 big-endianとした。
- 設計commit: 25686185048724c8310ff29f137a734bb2bb06e2
- テストcommit: 98bac82f70a85ddaff07e0dc5148abeac22a77cc
- root STATUS追記commit: d1ee89e8567a4c7a2bcb3559a2e4daf252e733bc
- 監査ブランチHEAD: d1ee89e8567a4c7a2bcb3559a2e4daf252e733bc
- 正式状態は設計レビュー要修正を維持。実機検証・設計承認・実装承認は未了。実装・テスト実装・PR・本番反映は禁止。
