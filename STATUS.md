# STATUS.md

最終更新: 2026-11-23 (JST)
対象: Issue #31「予測データの再現性・保存整合性を監査する」
監査ブランチ: `tmp-audit-31-repeats`

> このファイルは、Issue #31 の監査経過を引き継ぐための資料であり、正式な進捗ボードではない。正式な正本は `docs/status/STATUS.md`。本ファイルの更新は監査ブランチ上の引き継ぎ資料への追記に限定する。Issue #31 本体、予測ロジック、本番DB、`docs/status/STATUS.md` は変更していない。

## 1. 現在の結論

重複保存された109件の再実行元について、全件の参照IDとバッチ再実行フラグを静的pickle opcode解析で確認した。再実行元レコードは次の内訳で確認できた。

- 72件: DB世代705で参照先を直接発見し、子レコードと `race_key`、`settings_hash`、`seed`、`trials`、`raw_text` SHA-256 が一致。
- 37件: DB世代705では見つからなかったが、過去DB世代489および490の両方で参照先を発見。37件すべてで `race_key`、`seed`、`trials`、`raw_text` SHA-256 が一致。

よって109件すべてについて、参照先となる再実行元履歴の実在を、世代705または過去世代のDBで確認できた。37件の参照元は世代489・490の時点では存在し、世代705では不在である。

**更新（2026-11-23）:** Gen675/676の全行差分で、Ver322〜324の履歴1,365行と周回スナップショット8,196行だけが消え、Ver325〜328は全件残存・同一内容だった。Gen676直前のapp.pyには、このバージョン選択に基づき旧Verをアーカイブ表へ移して本体からDELETEし、別SQLiteへ書き出した後にアーカイブ表をDROP+VACUUMする機能が存在した。データ差分はこの機能と非常によく一致するため、**最有力原因は旧Verアーカイブ・本体削除機能の実行**へ更新する。ただし、実際のUI操作・選択keep_versions・出力されたバックアップファイルの保存状態は記録がなく、操作実行そのものは未証明。

**未確定:** 実際に押されたUI操作とkeep_versions、アーカイブ別ファイルの現存・保管場所。Job 368〜370と個別レース履歴の直接対応。時間窓の一致は相関であり、直接JOINではない。

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

### Gen490〜Gen705間の中間世代監査（2026-11-23）

Gen491〜496を読み取り専用で追加監査した。さらにGen496（既知の37/37存在）とGen705（既知の0/37不在）を端点に二分探索し、37件の元履歴IDを直接照合した。

#### Gen491〜496の全件照合

| 世代 | 保存時刻（JST） | DBコミットSHA | manifest blob SHA | DB SHA-256 | root |
|---:|---|---|---|---|---:|
| 491 | 10/15 21:39:30 | `adfab799ba2c01cb0183d3dcd03eb4c5f0e0f1cd` | `357f469398486d9d7154884256cc8b594c5ff237` | `4d233f8bac11f33aa7ad107602faf1303ae5823913d97318e4379161a6578e46` | 37/37 |
| 492 | 10/15 22:06:26 | `6c45163fa840399f00e2d3903b8d84f49e18c11e` | `b61c182ceb2b1faaa060e4794424a7fc0a6b690b` | `397edeaafdd3996bd9922f42befcd362351724dadb4b7b2270ce87313418d57d` | 37/37 |
| 493 | 10/15 22:37:29 | `d21a792f5074b171429502bf4b17cacdbc95995a` | `d8788fc03b4eda2812b8dc02bcfc6fe41914b29a` | `b1de799a21e2947f36a3d7d68f564f3bd34700329dc5ba07def0669a54b4d44d` | 37/37 |
| 494 | 10/16 00:44:03 | `09bf2fb47463d35f95ccf6412bcee228a08fda2c` | `e867fbca25efa05d5a0cf7f546c539d0a8c67546` | `92d08258a140c24b201caffd256ed323f231fcd99709c3294778c164127c5c0f` | 37/37 |
| 495 | 10/16 09:56:03 | `188b336c05b320b3a6f2af7aa1b3f52ac8b19b87` | `89ebc3884191e6d4d6535782a913a648491172d8` | `4f28160c62bb8042566e1383400fcfabb52aad79aea023d39813b8ea7bb3b1c9` | 37/37 |
| 496 | 10/17 08:33:43 | `1f398fd69af9f0af25abb98c9b1a35b062ebdaa7` | `76955411294cf2208a068bedee48a2af83979464` | `69e5eabdd5f3f5f885f3e6fe5c9c774347224b8bba9652bb82533a15170e6a72` | 37/37 |

全世代で `integrity_check=ok`、SQLite read-onlyで監査。37件すべてについて `race_key`、`seed`、`trials`、`raw_text` SHA-256が一致。元履歴側の `app_version=Ver323` と `settings_hash=13a3b266ac35fdad` は、子側Ver326 / `6a6084b95aa3262e` と異なる。

Run: [#45112544906](https://github.com/memememe0110/AutoRaceAI/actions/runs/45112544906)、SUCCESS、artifact `14789274909`。

#### 二分探索で照合した世代

| 世代 | 保存時刻（JST） | DBコミットSHA | manifest blob SHA | DB SHA-256 | root |
|---:|---|---|---|---|---:|
| 600 | 10/23 03:03:25 | `8b4df1ab8f0faeca8ae687502c674ee4e838ae3f` | `3d2f2ddf6d298e88547fb779ed48fbcb985d3e26` | `deafb0a69220a4490bd91afc18be52f8d58e209fb18bb40d44a0e23308cc8cda` | 37/37 |
| 652 | 11/01 14:30:54 | `c7a128222e18e3520835deeadba106f3c366ef68` | `401eaba40f9de252c0d97894fbf09932116faff3` | `17cafbba8a5adcd2ada741101e136d5a3c3cbcb91a250ab625fa9184c211ccbf` | 37/37 |
| 665 | 11/03 07:05:33 | `8aba536015263e614fcd4794411a7dbec0ac67c8` | `3849f8e4bf4e9cb7082f7edf760a2f0f48c44f1c` | `2ae2411fffa4112dc038b218aa97b4feb11824413871eb453e052779e74e4083` | 37/37 |
| 671 | 11/03 16:39:00 | `bf3287bbee6ed299631b3f0864c74c9f1931498c` | `cc06dd559ece4a0b0603052ca0a6594c150d8c25` | `2eb978f1a8fd493771f16effa969cb3c891c4b40bd8474190df2f81baff91d4e` | 37/37 |
| 674 | 11/03 20:56:15 | `56364723d8f90b666b4ec61abc4adcf84117e071` | `81ac7bc4e9cd578a3245795dc7840579a37d9527` | `24bab0e85799bf3d6fb5102db87b9d032f2ab35f9adc70b0f09f7aee299fb29b` | 37/37 |
| **675** | **11/03 21:56:34** | `590d5e4c45f6c6d53e89b928ef68a19b89c68011` | `1d193b3d53ae7f1bf3d60dc6fe369b6f8ef5102b` | `d5e7c141ae0e66708a53b2e8ac4d7035d019aeaf98cba596bc83d9d2d128d7ac` | **37/37** |
| **676** | **11/09 18:50:22** | `c9e300b210da0d6d262b16d0c5527f8626f13d7e` | `aa039f6ba6f885f2ebbb8a67e06de5eb9ced8e12` | `72b99a0a92be5255c1f87b03823c4a2f41ba8e6514a5b1f1be1eb88c305cdeda` | **0/37** |
| 678 | 11/10 12:21:42 | `3a78a9c3a90a1b750237d16fa4473733746ea13b` | `5fef57fdb3f52a3ea57ae0679c8ccb5f5121b11e` | `ac77772865cf17dad33427698bc60c2b041598183a9f79f786af0cf90f76bf30` | 0/37 |

世代705も0/37（DBコミット `20c67a8450ea72d219c19601319faf558a2f93c1`、manifest blob `9b942915154eed0f7af9a4322e0a77c976fad1da`、保存時刻 11/16 02:20:42 JST、DB SHA-256 `57a6f7d4e8db23e9560e66ffbc2130f3cf1156164b3d760513c3da33f7535e67`）。

二分探索の監査Run: [#45113074767](https://github.com/memememe0110/AutoRaceAI/actions/runs/45113074767)、SUCCESS、artifact `14791335379`。探索順はGen600、652、678、665、671、674、676、675。Gen600/652/665/671/674/675で37/37、Gen676/678で0/37。

**現時点の境界:** 最後に37件が存在した監査済み世代はGen675、最初に不在を確認した世代はGen676。Gen675とGen676はDB世代として連続し、Gen676 commitの親はGen675 commit。Gen676の `autorace_players.sqlite3.chunks.previous.json` はGen675のmanifestそのもの（blob SHA `1d193b3d53ae7f1bf3d60dc6fe369b6f8ef5102b`）を保持する。

**探索上の留保:** 497〜599、601〜651、653〜664、666〜670、672〜673の全DB本体は個別照合していない。したがって、未検査世代で一時的な消失と再出現がなかったことは未証明。Gen676は「最初に不在を確認した世代」であり、最初の消失世代と断定するにはこの留保を残す。Gen675→676の隣接境界は直接照合済み。

#### Gen675からGen676で起きたDB内容の変化

- Gen676のcommit messageは `AutoRaceAI: DBを手動保存（軽量）`。親commitはGen675。
- Gen675: DBサイズ 988,090,368 bytes、118 parts、uploaded_parts=19、reused_parts=99。
- Gen676: DBサイズ 924,971,008 bytes、111 parts、uploaded_parts=111、reused_parts=0。全chunkが新規で、DB全体SHA-256も変更。
- Gen675とGen676のmanifest `latest` は両方 `2026-11-03T21:51:01.430098+09:00`。11/9保存時点でも、DB内の最新履歴時刻は11/3のまま。
- `v231_prediction_history`: 4,069 → 2,704（−1,365行）。
- `v252_lap_prediction_snapshots`: 18,822 → 10,626（−8,196行）。
- 取得したmanifest統計で他の主要テーブル件数はGen675/676間で変化なし（例: `prediction_snapshots` 9,021、`race_history` 26,850、`v222_prediction_restore` 1,084は同数）。
- Gen677のmanifestは `v231_prediction_history=2,706`、`v252_lap_prediction_snapshots=10,638`。Gen677の37 root本体照合は未実施。Gen678では0/37を再確認。

この差分は、Gen676保存時に少なくとも2つの履歴系テーブルが大幅に縮小したことを示す。**最有力仮説は、Gen676の手動DB保存時に、すでに履歴が間引かれた／古いDB状態が保存されたこと、または保存前に対象テーブルの整理・置換が行われたこと。** ただし、手動保存処理自体が削除を実行したのか、保存元DBがすでに削除済みだったのかは未証明。DB branchのGen676 commitはmanifest/chunkの変更のみで、アプリケーションコードの変更ではない。

#### Gen675/676の全行差分と削除パターン（2026-11-23）

Run [#45121675771](https://github.com/memememe0110/AutoRaceAI/actions/runs/45121675771) で全行差分を実施。追加分類・アーカイブ表棚卸しはRun [#45123549751](https://github.com/memememe0110/AutoRaceAI/actions/runs/45123549751) で再実行。両Runともテスト・DB取得・read-only照合・artifact保存がSUCCESS。最終artifact `14794503868`。

- Gen675: 4,069行、Gen676: 2,704行。
- 削除1,365行、追加0行、両方に残った2,704行は全列ハッシュ一致（changed_retained=0）。
- 削除されたhistory_idは5409〜8263。37 root ID（5856〜5895の対象37件）は37/37すべて削除行に含まれる。
- 削除1,365行はVer322=447、Ver323=447、Ver324=471のみ。Gen676に残る2,704行はVer325=14、Ver326=1,650、Ver327=460、Ver328=580。
- 削除行のsimulation_modeは全1,365行が `6周内蔵型壁展開`。
- 削除行のprediction_timeは2026-09-30 23:15:59〜2026-10-20 21:31:58 JST。残存行は2026-10-04 22:11:12〜2026-11-03 21:50:52 JST。時間帯が重なるため、単純な日時カットオフでは説明できない。
- settings_hash内訳: Ver322は445行 `abbee8aed54a2db6` + 2種類各1行、Ver323は445行 `13a3b266ac35fdad` + 2種類各1行、Ver324は471行 `ed185f5c873307fd`。
- 37 rootは全件Ver323、settings_hash=`13a3b266ac35fdad`、simulation_mode=`6周内蔵型壁展開`、seed=20260719、trials=20000、venue_override空欄。prediction_timeは10/2 12:35:29〜12:58:39 JST。

周回スナップショットのapp_version別件数も同じパターン:
| app_version | Gen675 | Gen676 | 差 |
|---|---:|---:|---:|
| Ver322 | 2,682 | 0 | −2,682 |
| Ver323 | 2,682 | 0 | −2,682 |
| Ver324 | 2,832 | 0 | −2,832 |
| Ver325 | 84 | 84 | 0 |
| Ver326 | 6,180 | 6,180 | 0 |
| Ver327 | 2,076 | 2,076 | 0 |
| Ver328 | 2,286 | 2,286 | 0 |
| 合計 | 18,822 | 10,626 | −8,196 |

Gen675・Gen676とも `v319_prediction_history_archive` と `v319_lap_snapshots_archive` は存在せず、両DBともread-only、query_only=ON、integrity_check=ok。archive表は世代スナップショットには残っていない。

#### Gen676直前のアプリコードに存在した旧Ver整理機能

Gen676保存時刻は2026-11-09 18:50:22 JST。直前のapp.py更新はcommit `ea9be46d094ae2c6b4a139919ecc34a5819de40f`（11/9 12:49 JST）。この時点のコードでは `APP_VERSION=Ver326`、`_V231_APP_VERSION=Ver326`。

GitHub上の当該app.pyで以下を確認した（ソースコードの存在確認であり、実際の操作実行ログではない）。

1. `_v319_archive_old_version_predictions()`（app.py 36393〜36471行）は、UIで選択された `keep_versions` 以外の `v231_prediction_history` 行を `v319_prediction_history_archive` にINSERTした後、本体からDELETEする。同じ条件で `v252_lap_prediction_snapshots` も `v319_lap_snapshots_archive` へ退避して本体からDELETEする。
2. UI（37611〜37632行）は「DBに残す予測バージョン」のmultiselectを表示し、選択外のVerを退避する。デフォルト保護リストにはVer322/323/324も含まれるため、Gen676の結果をこの機能で作ったなら、操作時にVer322/323/324をkeepから外した必要がある。
3. `_v319_export_and_drop_archives()`（36305〜36362行）はアーカイブ表を `<DB名>_old_versions_backup.sqlite3` に書き出し、メインDBからarchive表をDROPしてVACUUMする。
4. 「現在のDBをGitHubへ保存（軽量）」UI（37650〜37654行）は `push_db_to_github(...,_lightweight=True)` を呼ぶ。push関数（36491行以降）のlightweight分岐は現在DBのchunk uploadだけで、リモート統合や予測履歴DELETEを行わない。

**最有力仮説:** UIでVer322/323/324をkeep対象から外して旧Ver予測をアーカイブし、アーカイブを別SQLiteへ書き出して本体からDROP+VACUUM、その後に手動軽量保存した。この操作順なら、Gen676で観測した「Ver322〜324だけのhistory 1,365行減」「同じ3バージョンだけのlap snapshot 8,196行減」「他Verは完全維持」「軽量保存というcommit message」を一貫して説明できる。

**未証明:** UIの実操作ログ、選択されたkeep_versions、アーカイブ処理の戻り値（archived/deleted件数）、`_old_versions_backup.sqlite3` の実在・保管場所。Gen676にarchive表がないことは、export/drop後の状態と整合するが、バックアップファイルが保存・保全された証明ではない。したがって、コード上の削除経路とDB差分は強く一致するが、操作実行の断定はしない。

#### Job 368〜370との時間照合

- Job 368: 10/15 12:30:27〜21:04:47 JST。Gen491は21:39:30保存（終了35分後）で37/37存在。
- Job 369: 10/15 21:13:45〜10/16 00:15:17 JST。Gen491〜493は実行中に保存され、いずれも37/37。Gen494は終了後の00:44:03保存で37/37。
- Job 370: 10/16 00:17:34〜09:07:11 JST。Gen494は実行中、Gen495は終了後09:56:03保存で、いずれも37/37。
- Gen496（10/17 08:33:43）も37/37。
- その後もGen600（10/23）、Gen652（11/1）、Gen665/671/674/675（11/3）まで37/37が残っている。

よって `force_current=1` を含むJob368〜370は109件の重複保存の有力説明ではあるが、**37 rootの消失を直接起こした操作とは確認できず、時系列上もGen675まで元履歴は残存**している。Job370終了からGen676保存までは約24日9時間43分。遅延処理等の証拠がない限り、Job368〜370をGen676の消失原因とする説明は弱い。

#### 監査Workflowの実行・変更記録

- commit `d1ab447e2cf2c49f433c1a09c97f107c8b627629`: 中間世代のread-only監査Workflowを追加。
- Run #45112544906: Gen491〜496監査、SUCCESS、artifact `14789274909`。
- commit `68f08148d9027a5348e40e6a083c3e81e9cea591`: Gen497〜705のcommit mapを解決し、二分探索するWorkflowへ拡張。
- Run #45113074767: 二分探索監査、SUCCESS、artifact `14791335379`。
- Gen675/676だけを監査する別Workflowをcommit `b9a672c76b513fee32bb31eb1869b40aa7f9722f` で一時追加したが、意図したWorkflow runは起動しなかったため、未使用ファイルをcommit `29fd7597e4c3583a99ae6fe29810f15b3ce01304` で削除。境界判定は上記Run #45113074767で完了しており、この未起動Workflowには依存しない。
- push時に既存 `issue-31-historical-roots.yml` がRun #45113073347、#45114096211として起動したが、いずれもjobs=0。DB監査jobの失敗ではない。原因は未調査。
- DB本体の取得・照合は全てread-only、`integrity_check=ok`、`pickle.loads` / `pickle.load` 未使用。


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

1. 必要ならGen677を追加でroot照合し、Gen676以降での再出現がないことを確認する（Gen678・705は既に0/37）。
2. Gen497〜675の未検査世代について、manifestの `v231_prediction_history` / `v252_lap_prediction_snapshots` 件数と `latest` を時系列走査し、Gen676以前に別の大きな巻き戻し・再構築がないかを確認する。これはDB本体の全件照合とは区別する。
3. 旧Ver整理機能の操作記録・選択keep_versions・`_old_versions_backup.sqlite3` の現存場所を確認する。特に、アーカイブ出力ファイルがStreamlit実行環境またはユーザー保管先に残っているかを調べ、復元可能性を評価する。
4. Job368〜370とrace_keyの直接対応を示す永続データがないか、workerログ・結果blobの静的調査を継続する。pickle payloadの実行・デシリアライズは禁止。
5. `force_current=1` は重複生成の有力候補として保持するが、Gen675までrootが残存した事実から、root消失の直接原因とは分けて扱う。
6. 直接原因が特定できた後、ユーザー承認を得てIssue #31と正式な `docs/status/STATUS.md` へ反映する。現時点で正式ボードは未変更。


#### GitHub Release上のGen675完全一致DBバックアップを確認（2026-11-23）

GitHub Releases一覧でRelease `db-20261106-001313` を発見。Release本文は `Built from 590d5e4c45f6c6d53e89b928ef68a19b89c68011 (immutable)` と記載し、Gen675 DBを保存したコミットSHAと完全一致する。

- Release: https://github.com/memememe0110/AutoRaceAI/releases/tag/db-20261106-001313
- Asset: `autorace_players.sqlite3`
- Asset size: 988,090,368 bytes
- Asset SHA-256: `d5e7c141ae0e66708a53b2e8ac4d7035d019aeaf98cba596bc83d9d2d128d7ac`
- Gen675監査記録のDB SHA-256: 同じく `d5e7c141ae0e66708a53b2e8ac4d7035d019aeaf98cba596bc83d9d2d128d7ac`
- Release published: 2026-11-06T00:13:14Z

**確定:** Release assetはGen675 DBとSHA-256が一致するため、Gen675の完全DBスナップショットそのもの（バイト単位で同一）である。Gen675では37件の元履歴が37/37存在し、Gen675→Gen676の差分で削除されたVer322〜324の履歴1,365行・周回スナップショット8,196行もGen675側に存在することが既存のread-only監査で確認済み。したがって、旧Ver整理前の完全DBバックアップはGitHub Release上に現存する。

**区別:** これはアーカイブ処理が出力する `autorace_players_old_versions_backup.sqlite3` そのものではなく、旧Ver整理前のメインDB全体 `autorace_players.sqlite3`。別SQLiteアーカイブファイルの現存は依然未確認。ただし、37 rootと削除対象行を検証するための復元元データは確保できている。

**次の安全な手順:** Release assetを取得し、read-onlyでSHA-256再検証、`integrity_check`、37 root ID、Ver322〜324の履歴1,365行と周回snapshot 8,196行を照合する。必要ならGen675 DBから削除対象だけを別の新規DB/CSVへ抽出する。Gen676や本番DBへの上書き・復元は行わない。

GitHub code searchでは `autorace_players_old_versions_backup.sqlite3` および `old_versions_backup sqlite3` は検索結果なし。これはGitHubリポジトリ検索上で見つからなかったという意味に限り、Streamlit runtimeやローカル保存先に存在しない証拠ではない。


#### Gen675 Release assetの独立取得・read-only再照合完了（2026-11-23）

Release asset `db-20261106-001313/autorace_players.sqlite3` をGitHub Actions runnerで実際にダウンロードし、Gen675監査時の値と独立に再照合した。

- Workflow Run: [#45144072912](https://github.com/memememe0110/AutoRaceAI/actions/runs/45144072912)、SUCCESS
- 実ダウンロード後SHA-256: `d5e7c141ae0e66708a53b2e8ac4d7035d019aeaf98cba596bc83d9d2d128d7ac`（期待値と一致）
- 実ファイルサイズ: 988,090,368 bytes
- SQLite `integrity_check=ok`
- SQLite接続: `mode=ro`、`PRAGMA query_only=ON`
- `pickle.loads` / `pickle.load` 未使用
- Ver322〜324の `v231_prediction_history`: 1,365行を再確認（Ver322=447、Ver323=447、Ver324=471）
- Ver322〜324の `v252_lap_prediction_snapshots`: 8,196行を再確認（Ver322=2,682、Ver323=2,682、Ver324=2,832）
- 37 root ID: 37/37存在し、各行の `race_key`、`seed`、`trials`、`raw_text` SHA-256を参照元定義と照合して37/37一致

**結論:** Release assetはGen675完全DBであることを、今回の独立ダウンロード・SHA再計算・SQLite integrity・row counts・37 root内容照合で再確認した。Gen675→Gen676で削除された対象データは、Gen675 DBから別CSVへ安全に抽出済み。

Workflow artifact: [issue-31-gen675-verified-old-version-data-45144072912](https://github.com/memememe0110/AutoRaceAI/actions/runs/45144072912)（artifact ID `14803252269`、30日保持、2026-12-23 09:06 UTC期限）。内容:
- `recovered_v231_history_Ver322-324.csv`（1,365行、raw_text/payload等の大容量内容は除外）
- `recovered_v252_lap_snapshots_Ver322-324.csv`（8,196行）
- `root37_verification.json`
- `summary.json`

これは別ファイルへの抽出であり、Gen675元DB・Gen676 DB・本番DBのいずれも変更していない。復元・上書きは実施していない。


#### Gen675→Gen676 cleanupの使い捨てSQLite再現完了（2026-11-23）

- Workflow Run: [#45146199262](https://github.com/memememe0110/AutoRaceAI/actions/runs/45146199262)、SUCCESS（test/reproduce両ジョブ成功）
- 実行commit: `2864c721aba4b7da97850fb6992a3e9c725aacd4`
- Gen675 Release DBを取得し、SHA-256 `d5e7c141ae0e66708a53b2e8ac4d7035d019aeaf98cba596bc83d9d2d128d7ac` を再確認。
- Gen676 DBもcommit `c9e300b210da0d6d262b16d0c5527f8626f13d7e` から取得。SHA-256 `72b99a0a92be5255c1f87b03823c4a2f41ba8e6514a5b1f1be1eb88c305cdeda`、integrity_check=ok。
- Gen675/Gen676両DBはread-only、query_only=ONで検査。pickle.loads/load未使用。
- Stage 1: Gen675のVer322/323/324履歴1,365行とLap snapshot 8,196行をアーカイブ表へ退避し、メイン表から削除。退避件数はそれぞれ1,365/8,196。
- Stage 1後のメイン表は履歴2,704行、Lap snapshot 10,626行。
- Stage 2: アーカイブ表を別SQLite `old_versions_archive_reproduction.sqlite3` へexportし、メインDB側のアーカイブ表をDROP、VACUUM。
- Stage 2後のメインDB対象2テーブルはGen676と行単位digestが両方一致:
  - `v231_prediction_history`: 2,704行、SHA-256 `65aa7bcfe54c47be371628293aea1c30e9605cac11a7d5f40e137248e078750b`
  - `v252_lap_prediction_snapshots`: 10,626行、SHA-256 `ca30e3e4661702f3fb4794acf2986d0f03756bf8c98a6a71ab7d7126119bac1f`
- 別SQLiteへのexport結果: 履歴1,365行、Lap snapshot 8,196行。root履歴37/37を保持。
- main DB / archive DBのintegrity_checkはいずれもok。
- ログ上 `production_db_touched=false`、`gen675_gen676_modified=false`。
- Artifact: [issue-31-gen675-cleanup-reproduction-45146199262](https://github.com/memememe0110/AutoRaceAI/actions/runs/45146199262/artifacts/14803977379)、ID `14803977379`、サイズ56,816,154 bytes、30日保持。

**判定:** Gen675→Gen676で観測した対象テーブルの削除後状態は、旧Verアーカイブ→本体DELETE→別SQLite export→archive表DROP→VACUUMの手順で、使い捨てDB上に再現できた。対象2テーブルはGen676と行単位ハッシュ一致。これはデータ状態の再現成功であり、2026-11-09に実際にこのUI操作が行われたことや、当時のアーカイブファイルが現存することを証明するものではない。

**変更範囲:** 監査スクリプト・テスト・Workflowのみ。予測ロジック、save/restore本番コード、app version、本番DB、Gen675/Gen676は変更なし。正式な `docs/status/STATUS.md` とIssue #31も未変更。


#### 専用旧Verアーカイブの保存先・実行記録に関する読み取り調査（2026-11-23）

調査対象はGitHubリポジトリ、Gen676 commit tree、GitHub Releases、GitHub Actionsの実行履歴・関連Artifact。外部のStreamlit Cloud runtime、ユーザー端末、ローカルファイルシステムにはアクセスしていない。

**確認結果:**

- Gen676 commit `c9e300b210da0d6d262b16d0c5527f8626f13d7e` のrecursive treeに、`*_old_versions_backup.sqlite3`、`old_versions_backup` に該当する専用アーカイブファイルは見つからなかった。DB本体・chunk・manifestは存在する。
- GitHub Releases一覧で確認できたReleaseは `db-20261106-001313` のみ。AssetはGen675完全DB `autorace_players.sqlite3` であり、専用の旧VerアーカイブSQLiteではない。
- Gen676保存日（2026-11-09 UTC）に作成されたGitHub Actions workflow runは、repository APIのcreated日付指定検索で0件。
- 近接するRun #42478283739（11/8）のArtifactは `axis-a-20260726_飯塚_2R-6`（1,599 bytes）、Run #42852514980（11/10）のArtifactは `ver330a-lap1-lap1_v331_p1-13`（2,258,293 bytes）。どちらも旧VerアーカイブSQLiteを示す名前・用途ではない。
- Gen676直前のapp.py commit `ea9be46d094ae2c6b4a139919ecc34a5819de40f` の後、DB保存時刻（11/9 18:50:22 JST）より後の11/9 20:12 JSTにapp.pyのみを変更するcommit `386efbb5f7178cecb8cb3f46f4586b34bd66a3ca` が存在する（10 additions / 8 deletions）。これはコード更新履歴であり、旧Ver整理UIの実行記録ではない。
- GitHub code searchでは `old_versions_backup.sqlite3`、`_v319_export_and_drop_archives`、`archive_old_version_predictions` の一致は見つからなかった。コード検索は現行default branchを対象とするため、過去のローカルruntimeファイルの不存在証明にはならない。

**判定:** GitHubリポジトリ、Release、今回確認したActions実行・Artifactの範囲では、専用アーカイブSQLiteおよびGen676当日の旧Ver整理操作ログは**未発見**。Gen675の完全DBバックアップはReleaseに現存するが、専用アーカイブファイルとは別物である。

**未調査・アクセス不能:** Streamlit Cloudのruntime filesystem、runtime logs、ユーザー端末/ダウンロード先、外部ストレージ、ブラウザ操作履歴。したがって「保存されなかった」「操作されなかった」とは結論しない。操作ユーザー・keep_versions選択値も未確認のまま。

**変更範囲:** 本調査はGitHub上の読み取りのみ。現時点ではSTATUS.mdを含むリポジトリファイル、Issue、正式な進捗ボード、本番コード、DBを変更していない。


## 7. 旧Ver整理の安全化設計ドラフト作成（2026-11-24）

### 作業開始時点
- 対象ブランチ: `tmp-audit-31-repeats`
- 作業開始時HEAD: `1fa13ac9d7d4d21e164e8931453563f76935ec9f`
- 対象ソースコミット: `ea9be46d094ae2c6b4a139919ecc34a5819de40f`
- 対象ソース: `app.py`（blob SHA `ce637255569886f4c59a493f09964819ec7d2f00`）
- 実コードblobを取得し、対象関数・UIの行番号と例外処理を再確認した。行番号は当該コミットのapp.pyに対する1始まり。

### ドラフト作成完了
以下の3文書を監査ブランチへ追加した。
1. `docs/audit/issue31-current-archive-spec.md`
2. `docs/audit/issue31-safe-cleanup-design.md`
3. `docs/audit/issue31-cleanup-test-plan.md`

設計文書commit:
- SHA: `334c29a1af52ba1f7e17efe1cc786ec34316395e`
- 変更ファイル: 上記3文書のみ
- commit message: `Add Issue #31 archive safety design docs`

### 実コード再照合で確定した事項
- `_v319_archive_old_version_predictions()`: `app.py:36393-36471`
- `_v319_export_and_drop_archives()`: `app.py:36305-36362`
- UI: `app.py:37611-37654`
- archive処理はhistoryをarchiveへINSERTする際にhistory_id重複を除外するが、archive INSERT件数とDELETE件数の一致検証はない。
- snapshot archive INSERTにはhistoryと同じ主キー重複除外条件がない。
- archive関数は36463行でcommitし、VACUUM失敗は握りつぶしてreportをokにし得る。
- export関数は既存の出力先SQLiteをunlinkしてから作成する。出力件数・主キー・行ハッシュ・SHA-256・integrity_check・別DB復元検証は実装されていない。
- export関数はarchive表をATTACH先へCTASコピーし、メインDB側archive表をDROPしてcommitする。DETACH失敗は握りつぶし、VACUUM失敗はreportへ記録するがokを維持し得る。
- SQLite接続のwith contextはcommit前の未commitトランザクションをrollback対象にする。一方、既存archiveファイルのunlinkはSQLite transaction外であり、DB rollbackでは復元されない。commit後のDROPを戻す補償処理もない。
- Gen675→Gen676の対象差分は、これらの処理が作る状態と整合する。ただし、当時のUI操作・keep_versions・archive出力の実在は未証明。

### 未確定事項
- 2026-11-09の操作者、実際のUI操作順、keep_versionsの値。
- 当時のarchive処理reportと実行時例外の有無。
- `_old_versions_backup.sqlite3` の作成・保存・現存。
- 実運用時に処理がどの段階まで完了したか。

### 安全化設計の中核
- S0〜S7の状態遷移を定義。
- operation_idとplan_digestで対象集合・DB identity・承認を固定。
- 件数、主キー集合、全行ハッシュ、archive SHA-256、integrity_check、別DB復元を全てPASS必須とする。
- 検証済みoperation_idに結び付いたreceiptをDELETE API内部で再検証する。
- 検証PASS前にDELETE SQLへ到達できない単一入口設計とする。
- 失敗・中断・部分成功・重複実行・リトライ・承認後変更をテスト計画に含める。

### 変更範囲
- 変更したのは上記3つの監査・設計文書と、このルートSTATUS.mdのみ。
- 本番コード、本番DB、`app.py`、予測ロジック、買い目、EV、回収率計算、`APP_VERSION` は変更していない。
- Issue #31、正式な `docs/status/STATUS.md`、既存Artifactは変更・削除していない。
- 実装・PR・本番反映は未実施。設計レビュー待ち。

### 次の作業
1. 最優先で「archive検証PASS前のDELETE経路が構造的に存在しない」ことを設計レビューする。
2. operation_id receiptの改ざん耐性、source DB identityの定義、SQLite attached DB/atomicity境界をレビューする。
3. 承認後に限り、使い捨てSQLiteでテストを実装・実行する。
4. 本番コード変更や本番DB操作は、別途設計承認と明示承認を得るまで行わない。

## 正式進捗正本・Issue #31 反映記録（2026-11-24）

正式進捗正本とIssue #31への反映を完了。

- 正式進捗ブランチ: `docs/status`
- 正式進捗ファイル: `STATUS.md`（GitHub表示: `docs/status/STATUS.md`）
- 正式進捗commit: `3ac7e4f0bf8f8f2d3448876f1531522bec765dab`
- 正式進捗ファイル: https://github.com/memememe0110/AutoRaceAI/blob/docs/status/STATUS.md
- Issue #31 comment: https://github.com/memememe0110/AutoRaceAI/issues/31#issuecomment-7133071531
- 監査設計文書commit: `334c29a1af52ba1f7e17efe1cc786ec34316395e`
- 監査ブランチ最終HEAD（本追記前）: `076c24776f09d055f9c203d3cb14b8a9f71a1f22`

Issue #31の正式判定は「主要監査完了・再発防止未完了」。37件のGen675→Gen676消失、旧Ver整理処理による行単位ハッシュ再現、Gen675由来Artifactによる回収・検証、109件重複保存との別事象性、実操作イベント等の未証明事項を正本とIssueへ反映した。

設計レビュー前の実装・本番反映は禁止。今回も `app.py`、本番DB、予測ロジック、`APP_VERSION` は変更していない。


## Issue #31 設計一次レビュー（2026-11-24）

**正式判定：設計の方向性は妥当だが、設計承認未了（要修正）。実装・本番反映は禁止。**

一次レビューでR1〜R6を必須修正とし、監査ブランチ `tmp-audit-31-repeats` の設計文書に追補する。

- R1: SQLite Backup API等による一貫snapshot、WALを含むDB identity/schema/PK・row digest、削除直前再照合。WAL・同時更新・不一致時はfail closed。
- R2: 認証済み操作者の明示承認。operation_id/plan_digest/一回限りnonce、有効期限・取消・再利用防止、対象/DB/計画変更時の失効。
- R3: DELETE前の外部永続ログ、COMMIT_UNKNOWN、再起動後read-only照合、結果確定まで再DELETE禁止。
- R4: 書き込みトランザクション開始後にPK/row digest/保護対象を再照合し、2表を同一トランザクションで削除。不一致・片側失敗は全体rollback。
- R5: Gen675由来37 rootを通常整理の本体保護対象とし、archive保存要件と分離。混入時は拒否。将来の削除は別設計・別の明示的追加承認が必要。
- R6: VACUUMを削除安全性の証明から分離。失敗はPOST_DELETE_MAINTENANCE_FAILEDとして独立記録し、archive/restore/ログを保全。
- テストT01〜T20を維持し、T21〜T28を追加。T16をVACUUM独立状態に差し替え。

### 修正後の状態
- 設計文書3点: 一次レビュー指摘を反映。設計承認は未了。
- 実装・テスト実装・本番反映: 未実施。
- 次の作業: 3文書の再レビュー。R1〜R6とT21〜T28に未解決が1つでもあれば承認しない。
- 禁止範囲: app.py、本番DB、予測ロジック、APP_VERSION、既存Artifactは変更しない。


## 修正反映Git記録（2026-11-24）

- 設計文書・補助STATUSの修正commit: `1d3210b22958c83db7acf8f8778924ad6171093c`
- 正式進捗正本の修正commit: `53b00494839aba9fe7fa952fc3d9d2ed322b7ff3`
- Issue #31コメント: https://github.com/memememe0110/AutoRaceAI/issues/31#issuecomment-7133364016
- 変更ファイルは監査設計3文書とルートSTATUSのみ。本番コード・DB・予測ロジック・APP_VERSION・既存Artifactは変更なし。
- 次の作業: 3文書の再レビュー。一次判定「要修正」を維持し、R1〜R6/T21〜T28の全項目解決まで承認しない。

## Issue #31 設計再レビュー追補（2026-11-24）

**正式状態：主要監査完了・再発防止未完了。設計レビュー要修正。設計承認・実装承認は未了。**

今回、監査ブランチの3文書を再取得し、前回の再レビュー指摘を反映した。更新対象は設計文書と進捗記録のみ。

### 今回具体化した仕様
- R1/T23: 読み取り専用接続、BEGINで固定した読み取りsnapshot、同一接続のSQLite Backup API、取得前後main/WAL metadata、fsync、snapshot SHA→schema→PK/row digestの検証順、source_db_identity構成、削除直前projection/digest一致条件を規範化。
- R2/T24: OIDC主体識別、MFA必須、256-bit nonce、10分期限、取消/使用済み/期限切れの追記専用永続ledger、HMAC承認証跡、operation_id/plan_digest/DB identity/nonce結合を規範化。
- R3/T21: DB内receipt表、PREPARED/COMMIT_PENDING/DELETE_COMMITTED等の状態、外部DELETE_INTENT fsync、COMMIT_UNKNOWN復旧判定表、commit後ログ失敗時の非DELETE復旧を必須化。
- R4: BEGIN IMMEDIATE、busy_timeout=5000ms、ロック→再照合→37 root確認→2表DELETE→receipt更新→commitの順序、rollback/拒否/同一ID再試行禁止を固定。
- T16: VACUUM失敗の独立状態へ一本化し、重複差し替え節を削除。
- T01〜T28: 共通証跡schemaを定義。T21〜T25にfixtureと期待結果を追加。
- R5/R6、T22、T26〜T28: 前回判定を維持し、意味変更なし。

### 自己レビュー結果
- R1〜R4とT16・T21〜T25の前回未確定表現を規範仕様へ置換。
- 3文書の役割を整理。current-archive-spec.mdは現行コードの監査事実のみを保持し、安全化後の仕様はsafe-cleanup-design.mdとcleanup-test-plan.mdを参照する。
- R5/R6とT22/T26〜T28は変更せず、重複記述だけ整理。
- 今回は文書上の自己レビューであり、SQLite実機試験・OIDC/MFA試験・fsync耐久試験・強制停止試験は未実施。設計承認・安全性実証とは扱わない。次工程は独立再レビュー。実装は禁止。

### 変更禁止・未実施
- app.py、本番DB、予測ロジック、APP_VERSION、既存Artifactは未変更。
- 実装、テスト実装、PR作成、本番反映は未実施。
- 次回レビューまで実装・PR・本番反映を禁止する。

## Issue #31 設計再レビュー自己確認・Git記録（2026-11-24）

**自己レビュー結果：文書上の仕様固定条件を満たした。独立再レビュー待ち。設計承認・実装承認は未了。**

### 監査ブランチ反映
- R1〜R4: SQLite snapshot/WAL/source_db_identity、OIDC/MFA/nonce、DB内receipt/COMMIT_UNKNOWN復旧、BEGIN IMMEDIATE排他順を規範仕様として固定。
- T16: VACUUM失敗の独立状態へ一本化。重複差し替え節なし。
- T21〜T25: commit停止・WAL競合・nonce・canonical typed digestのfixtureと期待値を具体化。
- R5/R6とT22/T26〜T28: 前回の文面・判定を維持。
- T01〜T28: 共通証跡schemaとDELETE SQL trace、状態遷移、対象digest、receipt、外部ledger、最終DB状態、削除可否を必須化。
- current-archive-spec.mdは現行実装の監査事実に限定し、将来仕様との境界を追記。

### 自己レビュー確認
- R1〜R4に「案」「要検討」「例示」「例えば」「例：」の未確定表現なし。
- R5/R6本文は前回文書と一致。
- T16はテスト表に1行のみ。
- T22、T26〜T28の既存テスト行は前回文書と一致。
- T21〜T25のfixture・期待値、T01〜T28共通証跡を確認。
- 3文書をGitHubから再取得して確認済み。

### 残る未実施事項
- SQLite/WAL実機競合試験、OIDC/MFA連携試験、fsync耐久試験、COMMIT_UNKNOWN強制停止試験は未実施。
- 独立再レビューと設計承認は未了。
- 上記は実証・承認上の未完了事項であり、文書内のR1〜R4の未確定表現ではない。

### commit記録
- R1〜R4/T21〜T28初回具体化: a9eae1d775e5a351842211ebdb25305738c52811
- SQLite snapshot/receipt復旧追補: 8c7ab403716cc6f7cb17fc3de24687987d115578
- canonical digest仕様: 2baa0c2d79346927b1e0642cf2ec7324c46b609c
- snapshot lock順序: 226dd2ba248f5bd9c873d06b2894b049bd22f574
- receipt recovery・source digest補完: 50649df711259cb3abade8f9dcf5a2226e29ac7d
- T22維持・T24分離: dfb5cf9b34033193b6eee2695cd6e4a6adc960a3
- T21〜T25 fixture具体化: 0b340b387306cb353ac5629c4428071980c9c3c9
- 本追記時点の監査ブランチHEAD: 0b340b387306cb353ac5629c4428071980c9c3c9

### 禁止・変更なし
app.py、本番DB、予測ロジック、APP_VERSION、既存Artifactは変更していない。実装・テスト実装・PR・本番反映は未実施。独立再レビューが完了するまで、これらを開始しない。

## Issue #31 独立設計再レビュー指摘の反映（2026-11-28 JST）

**正式判定：主要監査完了・再発防止未完了／設計レビュー要修正。設計承認・実装承認は未了。**

### 独立再レビュー
- レビュー対象: 監査ブランチHEAD `9131f427377f8cbd05d7b7856c872d35aa6a640b` の設計3文書。
- Issue #31 comment: https://github.com/memememe0110/AutoRaceAI/issues/31#issuecomment-7239754361
- 指摘: R3 receipt SHA-256単独の改ざん耐性、T25 REAL -0.0 fixture、R4比較readerのclose順、T25 BLOB fixture値不一致、source_projection_digestの行順、R2 MFA freshness/claim allowlist。

### 文書反映
- R3: receipt_digestに加え、外部secret manager鍵によるreceipt_hmac/key_idを必須化。状態遷移・復旧時にもHMACを再計算/検証し、未知鍵・鍵利用不能・不一致はfail closed。T19にdigest再計算を伴う改ざんケースを追加。
- R2: issuer別acr/amr allowlist、step-up再認証、auth_timeの300秒鮮度と未来方向60秒の許容範囲を規範化。T24に境界・古い認証・許可外claimを追加。
- R4: 比較snapshotのmetadata採取後、DELETE前にsource read-only接続をROLLBACK/closeし、destinationもcloseする順序を規範化。T01にWAL/DELETE journal両方の正常系、T23にrollback-journal reader競合を追加。
- R1/T25: projection digestのtable/schema/row順序、PKv1/PROJv1 framing、PKなし・複合PK・WITHOUT ROWID・NULL許容PKの扱いを固定。virtual tableはcanonicalizationが未定義の間fail closed。
- T25: BLOB値をX'4142'へ統一。SQLite SQL bindで-0.0を格納した場合のreadback +0.0をgolden payload `0000000000000000`とし、入力側Python floatのbit patternを期待値に流用しない。

### Git記録
- R3/R2/R4初回反映: e7f04765187f6741cc4b9ccf2bc003e3a9fa70e4
- T19/T23/T24/T25テスト修正: ff91ea5a1244108229d782b958907ad97b33a33e
- projection framing/auth freshness補完: 14a14daf48d7606002fc2c11accbc6a72f67698b
- T01/T23両journal mode対応: 240dd0f1ea86e0e3202b1d5e865f712eba6e572a
- recovery HMAC/R4 receipt HMAC補完: 8c799f381f8bf14935e88931a533d67c2ce4c060
- 監査ブランチHEAD: 8c799f381f8bf14935e88931a533d67c2ce4c060

### 自己レビュー
- R5/R6本文は前回と一致。
- T16、T22、T26〜T28の行は前回と一致。
- R3 HMAC、R2 freshness、R4 reader close、PROJv1/PKv1、T19/T23/T24/T25の各修正をGitHubから再取得して確認。
- SQLiteの小規模挙動確認では、通常SQL bindしたREAL -0.0のreadbackが+0.0、IEEE754 payloadが0000000000000000となることを確認。実機の耐久・競合試験ではない。

### 未実施・禁止
SQLite/WAL実機競合、OIDC/MFA連携、fsync耐久、COMMIT_UNKNOWN強制停止復旧は未実施。独立再レビューの再確認と設計承認は未了。実装・テスト実装・PR・本番反映は引き続き禁止。app.py、本番DB、予測ロジック、APP_VERSION、既存Artifactは変更していない。
