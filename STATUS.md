# AutoRaceAI 進捗ボード

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
監査ブランチ現HEAD: `076c24776f09d055f9c203d3cb14b8a9f71a1f22`

**状態：設計段階。実装・本番反映は未実施。** `app.py`、本番DB、予測ロジック、`APP_VERSION` は変更していない。設計レビュー完了前に実装・本番反映へ進まない。

### 次の作業

1. 3設計文書のレビューと指摘反映。
2. レビュー承認後、別途明示承認を得て隔離環境で実装・試験を検討。
3. 実操作イベント・操作者・`keep_versions`・専用アーカイブの証拠は、別途取得できた場合のみ確定する。

結論：Gen675→Gen676の37件消失について、消失境界・技術的再現・回収可能性の確認は完了。実際の操作イベントの証明と再発防止策の実装・検証は未完了。



## 自律監査の進捗更新（2026-11-28 JST）

### 共通運用
- 現行Ver326を予測精度比較の固定基準とする。予測式・シミュレーション条件・買い目・EV・回収率計算は推測で変更しない。
- 読み取り監査、既存Artifact分析、非予測系テスト/監査スクリプトは専用ブランチ・PRで自律的に進める。
- 予測結果が変わる変更は、候補/影響/新バージョン/固定条件比較をまとめて事前承認。承認前に実装しない。
- 本番DB書き込み、mainへの直接push、Issue #28の測定条件・測定ファイル・作業ブランチ・過去データ変更は禁止。

### Issue #29 保存版整合性監査
- PR #49をmerge済み。merge commit: `43895d9c7c882a5c328b1421bcf9f37663933290`。
- read-only監査Workflow run `46150126357` SUCCESS。Artifact: `prediction-version-audit-46150126357`（ID `15265970427`）。DBはgeneration 705、commit `20c67a8450ea72d219c19601319faf558a2f93c1`、SHA256 `57a6f7d4e8db23e9560e66ffbc2130f3cf1156164b3d760513c3da33f7535e67`、integrity_check=ok。
- v187 plan 10,345行。app_version NULL 32行は全てlive/2026-08-02。source_prediction_version NULL 185行は全てlive/2026-08-02〜08-09。non-live originのsource_prediction_version欠損は0。
- v278 jobs 539行すべてapp_versionあり。save_version NULL 522行は記録したが、欠損/破損とは判定していない。17行のみofficial_importで明示save_versionあり。result_blobは新規decode/出力していない。
- 既存監査のhistory payload mismatch=0、queue version mismatch=0を維持。restore_without_history 55件、v266 orphan 50,230行、lap snapshot version mismatch 1,908行は未修復。過去DB行のUPDATE/DELETE/再生成なし。
- Issue #29はOPEN維持。次はVer330-A restore-only 1件 `20261114_山陽_4R` の保存経路と、v278 save_version NULL時の既定値決定を既存コード/テスト証拠で確認。

### Issue #32 Worker並行性監査
- PR #39 merge済み（merge commit `e284a1a19fc1825993be4dc75bca692bb2a0f473`）、Issue #32 CLOSED。
- Worker Global Isolation Tests `44117149226`、Real lap count simulation tests `44117149186`、Lap Snapshot Save Version Tests `44117149149` はSUCCESS。
- 追加の競合証拠が出ない限り完了扱いとし、#28測定には触れない。

### Issue #33 結果登録完全性監査
- PR #48 merge済み（merge commit `b92c7606e655cb310a91859b008f5143cc6e22c9`）。run `46149952713` SUCCESS。Artifact `result-registration-audit-46149952713`（ID `15265161636`）。
- generation 705の11件のentry-count由来goal-line flagは、全件でゴール線行数=finishers、全完走車が各記録済み周回に存在。完走車の欠落は0。出走数基準の過剰検出であり、真の周回漏れとは扱わない。
- 2件のresult_status誤りを確認: `20260723_伊勢崎_8R` 車番3は自落なのに通常、`20260803_川口_6R` 車番4は欠車なのにNone。
- 2件の登録不備を確認: `20261013_伊勢崎_6R` は公式結果があるのにentries/laps=0、payouts=9。 `20261025_伊勢崎_12R` は公式ニュースで実施確認、entries=8/payouts=11に対してlaps=0、race_time/start_time全件NULL。
- NULL auditのcutoff/naive ISO、start_time情報のみ、weather unknown、OtherRaceInfo no-joinの定義は変更していない。app.py/engine.py/DB/予測式は未変更。
- Issue #33はOPEN維持。次は該当4件の保存関数・パーサー経路と異常系テストを追跡。

### Issue #31 再発防止
- 正式状態は引き続き **主要監査完了・再発防止未完了／設計レビュー要修正**。設計承認・実装承認なし。
- 監査ブランチ `tmp-audit-31-repeats` HEAD: `d1ee89e8567a4c7a2bcb3559a2e4daf252e733bc`。SQLite/WAL実機競合、OIDC/MFA、fsync、COMMIT_UNKNOWN強制停止復旧は未実施。
- 独立再レビューと別途明示承認が完了するまで、Issue #31の再発防止実装・テスト実装・PR・本番反映は行わない。ほかのIssueの安全な読み取り監査は停止しない。

### 変更境界
- main最新HEAD: `43895d9c7c882a5c328b1421bcf9f37663933290`。
- 今回のmainマージは監査スクリプト/テスト/Workflow出力のみ。予測式、APP_VERSION、DBスキーマ、保存処理、本番DBは変更していない。
- Issue #28の測定条件・測定ファイル・作業ブランチ・過去データは変更していない。


## 追加監査・保存経路確認（2026-11-28 JST）

### Issue #29
- PR #50 merge済み: `a65f56e295e91eb7622c6b3f26e559612421701c`。Workflow run `46151064875` SUCCESS。Artifact `prediction-version-audit-46151064875`。
- generation705に `v319_prediction_history_archive` は存在せず、55 restore_without_historyに対するarchive matchは0。過去archive操作の不存在を意味するとは断定しない。
- app.pyの `_v319_clear_prediction()` はv231_prediction_history/prediction_snapshotsを消すが、v222_prediction_restore/v252_lap_prediction_snapshotsを同期削除しない。結果登録Workerの再予測失敗・予測なし結果保存経路から呼ばれる。restore-onlyの有力候補経路として追跡する。
- `20261114_山陽_4R`: restore Ver330-A at 15:20:30 JST、lap snapshot Ver326×6、history 0。job535（official_import, app_version Ver326, save_version Ver330-A, 13:57〜15:34 JST）の時間帯と整合。job result_blobはdecodeしておらず、race単位のtask証拠は未取得。
- 55 restore orphan、50,230 v266 snapshot orphan、1,908 lap snapshot version mismatchは未修復。Issue OPEN維持。

### Issue #33
- PR #51 merge済み: `e725f5462de3d666bd50972b7e9269be8b21a748`。Workflow run `46151371002` SUCCESS。Artifact `result-registration-audit-46151371002`。
- goal-line監査はfinishersとresult_entries上の非完走車を分離。反妨等でfinish=NULLでもゴール線通過する車を未知車番扱いしない。
- generation705の11 entry-count flagsは全て完走車の周回/ゴール線が整合。2件のresult_status誤り（自落→通常、欠車→None）、1件のentries/laps完全欠落（20261013伊勢崎6R）、1件のlaps/time欠落（20261025伊勢崎12R）を維持。
- engine.pyの現行経路では、v41_register_result()はvalidが空でも結果保存処理へ進む。compact lap parserのラベル期待値と公式ページの表記差、事故略記の正規化不足も候補として記録。
- Ver327候補（result parser/registration integrity）はIssue #33 comment `7240253744` に一括報告し、実装承認待ち。承認前にengine.py/APP_VERSION/結果保存処理を変更しない。
- Issue #33 OPEN維持。

### Issue #31
- 正式状態は **主要監査完了・再発防止未完了／設計レビュー要修正** を維持。監査ブランチHEAD `d1ee89e8567a4c7a2bcb3559a2e4daf252e733bc`。
- SQLite/WAL実機競合、OIDC/MFA、fsync、COMMIT_UNKNOWN強制停止復旧は未実施。設計承認・実装承認なし。

### 共通境界
- main HEAD: `e725f5462de3d666bd50972b7e9269be8b21a748`。
- PR #39（Issue #32）はapp.pyのWorker global isolation修正と回帰テスト/Workflowを含む。予測式・APP_VERSION・結果保存処理は変更なし。
- PR #48〜#51（Issue #29/#33）は監査スクリプト/テスト/Workflow出力のみ。app.py/engine.py、予測式、APP_VERSION、DBスキーマ、保存処理、本番DBは変更なし。
- Ver326を固定基準とする。予測結果が変わるVer327候補は承認待ち。
- Issue #28の測定条件・測定ファイル・作業ブランチ・過去データは変更していない。


### 追加のコード経路証拠と承認待ち（2026-11-28 JST）
- Issue #29: _v319_import_one_race() の _force_register_without_prediction() が _v319_clear_prediction() の後に結果のみ保存する。clear関数はhistory/prediction snapshotsを削除するがrestore/lap snapshotsを同期しない。55 restore-onlyの有力生成経路として記録。job535と20261114_山陽_4Rの時刻/Ver330-A save_versionは整合するが、race単位の実行ログは未取得。IssueコメントID `7240285554`。
- Issue #33: Ver327候補（周回ラベル互換、事故/欠車略記正規化、空結果の部分登録防止）を一括提示。result data/学習対象が変わり将来予測へ影響し得るため、engine.py/APP_VERSION/結果保存処理は明示承認まで未変更。承認依頼コメントID `7240253744`。
- Ver326を固定基準。Ver327候補は承認待ち。#28は引き続き完全対象外。
