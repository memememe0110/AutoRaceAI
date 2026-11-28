# Issue #31 旧Ver整理のテスト計画（レビュー用）

- 状態: 実装前テスト計画
- 対象: 安全化設計S0〜S7
- すべての失敗ケースで、archive検証・別DB復元検証PASS前の本体DELETEは禁止。
- 証跡はoperation_idに紐付け、メインDB外にも保存する。

## テストケース

| ID | ケース | 入力・注入条件 | 期待結果 | 本体削除可否 | 必須証跡 |
|---|---|---|---|---|---|
| T01 | 正常系 | journal_mode=WALとjournal_mode=DELETEの独立fixtureを用意し、各DBに2テーブルの既知件数候補行と保護行を作成。承認済み。archive/restoreの全検証を成功させる | 両journal modeでS0→S7。比較snapshotのsource/destination接続をDELETE前にcloseした後、件数・PK・行hash・SHA・integrity・復元が全一致し、2表DELETEとCOMMITが成功。削除後も保護行が残る | S5まで禁止、S6条件成立後のみ許可 | journal_mode、接続close証跡、preview manifest、approval、archive SHA、各検証結果、restore digest、削除前後件数、完了ログ |
| T02 | archive作成失敗 | archive作成先を読み取り専用または書き込み不能にする | S3で失敗。partial fileを隔離。エラーを記録 | 禁止 | errno/例外、path、operation_id、partial file情報、source identity |
| T03 | 容量不足 | archive書き込み中に容量不足を発生させる | S3失敗。既存archiveを保持し、再試行可能状態にする | 禁止 | 空き容量、書き込み位置、例外、partial size、既存archive SHA |
| T04 | ハッシュ不一致 | archive内の1行を改変、または1列値を欠落させる | S4で行hash不一致。検証レシートをFAILにする | 禁止 | 期待/実測hash、該当PK、差分列、archive SHA |
| T05 | integrity_check失敗 | archive DBを破損させるか、検査用DBに不整合を注入 | S4で `integrity_check != ok`。fail closed | 禁止 | integrity_check全文、DB SHA、例外、検証状態 |
| T06 | 中断・再開 | S3/S4/S5の各段階でプロセスkill、runner停止、接続断 | 中断状態を永続化。再開時にsource identity/plan digest/archive SHAを再検査。検証済みでない状態からS6へ飛ばない | S4・S5 PASS前は禁止 | 最終永続状態、kill時刻、再開ログ、各manifest、DB read-only照合 |
| T07 | 復元失敗 | archiveから別SQLiteへのコピー失敗、または復元後の件数/PK/hashを不一致にする | S5失敗。archiveは保持し、restore receiptはFAIL | 禁止 | restore error、restore DB SHA、integrity、期待/実測件数・hash |
| T08 | 重要履歴混入 | 保護対象37 root、または保護バージョンの行をdelete candidateへ混入 | S1またはS2で保護違反として拒否。承認ボタンを有効化しない | 禁止 | 保護ID一覧、混入件数、plan digest、拒否理由 |
| T09 | 重複operation_id | 同じoperation_idで異なる対象集合を要求、または同時に二重開始 | UNIQUE/lockで二重実行を拒否。対象変更はID衝突として拒否 | 禁止 | 既存operation record、競合要求、lock結果、拒否ログ |
| T10 | リトライ | S3/S4/S5失敗後、同一計画で再試行。さらにsource DBを変更して再試行 | 同一identity/planのみ再開可。source変更時は旧operation_idを失効し新IDを要求 | 検証PASSまで禁止 | retry_count、親operation_id、source identity比較、plan digest比較 |
| T11 | 部分成功 | historyだけarchive成功、snapshot失敗。あるいはDELETE時に片テーブルでSQLエラー | 全体FAIL。削除前なら本体無変更。削除トランザクション内なら両テーブルrollback | S6条件成立前は禁止。部分削除を成功扱いしない | テーブル別状態、transaction結果、rollback結果、行数 |
| T12 | 承認後の対象変更 | S2承認後にkeep_versions、対象PK、行値、DB identityのいずれかを変更 | plan digest/source identity不一致で承認失効。S3以降でもS6へ進めない | 禁止 | 承認時と実行時のdigest、差分、承認失効ログ |
| T13 | archiveファイルSHA不一致 | S4後、S5またはS6前にarchiveファイルを差し替える/1byte変更 | SHA再検査で不一致。receiptを無効化 | 禁止 | 期待/実測SHA、ファイルstat、検知時刻 |
| T14 | archive欠落・片テーブル欠落 | archive DBから片方の対象テーブルを削除、またはarchive fileを削除 | S4/S5でテーブル欠落として失敗 | 禁止 | schema一覧、期待/実測table一覧、file path |
| T15 | 既存archive保護 | 同名archiveが既に存在し、別operationを開始 | 既存ファイルをunlink/上書きせず、新規一意パスを使う | 禁止（新規archiveの全検証前） | 既存SHA、new path、作成ログ |
| T16 | VACUUM失敗 | DELETE commitとS7データ検証完了後にVACUUMのみ失敗 | データ状態DELETE_COMMITTED/POST_DELETE_VERIFIEDを維持し、保守状態のみPOST_DELETE_MAINTENANCE_FAILED。archive/restore済みDB/外部ledgerを保全。再試行はVACUUMのみ | 新規DELETE禁止。保守処理のみ再試行可 | 共通証跡、DELETE receipt、S7検証、vacuum_error、archive SHA、保全先 |
| T17 | 削除後検証失敗 | S6の削除commit後に残存件数/保護ID検査を意図的に失敗させる | SUCCESSにしない。archiveとrestore済みDBを保持し、復旧判断を要求 | 追加削除は禁止 | 削除commit receipt、post-check差分、archive SHA、復旧記録 |
| T18 | 未検証IDからDELETE | PREVIEWED/APPROVED/ARCHIVE_CREATED状態のoperation_idでDELETE APIを呼ぶ | API内部の状態・receipt gateで拒否 | 禁止 | 拒否されたoperation_id、状態、呼出元、gate判定 |
| T19 | 検証receipt改ざん | restore PASS記録、plan digest、archive SHA、stateを改ざんする。さらにreceipt_digestを攻撃者が再計算して整合させ、receipt_hmacは旧値のままにする | SHA-256再計算だけでは通過せず、receipt_hmac/key_id検証で拒否。鍵不明・鍵利用不能もfail closed | 禁止 | 改ざん前後receipt、再計算digest、HMAC/key_id検証結果、拒否ログ |
| T20 | 削除直前の同時更新 | S5 PASS後、削除直前に対象行を追加・更新・削除 | 最終再スキャンでpreviewとの差異を検出し承認失効 | 禁止 | preview/current PK digest・row digest比較、DB identity |



### T25固定fixture・期待canonical payload

fixture table名は digest_fixture、列順は id、v_null、v_empty、v_blob、v_text、v_int、v_real、v_negzero、v_jp、v_boundary。1行を次の値で固定する。
- id=1
- v_null=NULL
- v_empty=空文字
- v_blob=X'4142'（bytes 41 42）
- v_text="AB"
- v_int=整数1
- v_real=REAL 1.0
- v_negzero=REAL -0.0
- v_jp="競走"
- v_boundary="12345678"

期待storage classは順に integer、null、text、blob、text、integer、real、real、text、text。fixture投入後にtypeof()を読み、1列でも一致しなければfixture作成をFAILにする。
期待payloadは順に、id=0000000000000001、v_null=空payload、v_empty=長さ0のTEXT、v_blob=4142、v_text=4142、v_int=0000000000000001、v_real=3FF0000000000000、v_negzero=0000000000000000、v_jp=E7AB9FE8B5B0、v_boundary=3132333435363738。今回の通常SQL bindではv_negzeroへ-0.0を渡してもSQLite readbackは+0.0となるため、DBから読み戻したIEEE754 payload 0000000000000000をgoldenとする。canonicalizerはSQLiteから読み戻したREALのbit patternをそのまま符号化し、入力側Python floatのbit patternを期待値に使わない。v_blobとv_textはpayload bytesが同一でもtagが異なるため別digestになる。

## T01〜T28 共通証跡スキーマ（必須）

全テストで同じJSONL event schemaを使い、main DB外のappend-only test evidenceへ記録する。必須key:
- test_id、run_id、operation_id、event_id、event_seq、timestamp_utc
- state_before、state_after、actor_id_digest、process_id、worker_id、connection_id
- source_db_identity、source_snapshot_sha256、source_projection_digest、schema_fingerprint
- target_table、target_count、target_pk_digest、target_row_digest、protected_root_count、protected_root_digest
- archive_path_digest、archive_sha256、restore_receipt_sha256
- receipt_state、receipt_digest、external_ledger_event、external_ledger_fsync_result
- sql_trace（正規化SQL、対象表、時刻、transaction ID、実行回数、rowcount。値・秘密情報は記録しない）
- sqlite_error_code、exception_type、error_message_digest、rollback_result
- final_db_integrity、final_target_pk_digest、final_target_row_digest、final_protected_root_digest
- delete_allowed、delete_sql_count、expected_delete_count、actual_delete_count、final_verdict

証跡はoperation_id/test_idの複合キーで追跡する。各ケース終了時に最終DBをread-onlyで開き、integrity_check、対象PK/row digest、37 root digestを採取する。DELETE禁止ケースはdelete_sql_count=0。T01のみ全gate PASS後に各対象表DELETE SQLを1回ずつ許可する。証跡欠落・破損・連番飛びはテストFAIL。

## T21〜T28追加合格基準
- T21 recoveryはreceipt、候補PK/row digest、root digest、外部ledgerが全条件一致した場合だけ結果確定。同一operation_idのDELETE trace追加なし。
- T22は外部DELETE_INTENTのfsync完了前にDELETE SQLが0回。
- T23はWAL snapshot境界、取得前後metadata、BEGIN IMMEDIATE後の再照合を個別検証。
- T24は10分期限、MFA claim、取消・使用済みledger永続性を境界時刻で検証。
- T25は型fixtureごとのcanonical byte列と期待SHAをgolden valueとして固定。
- T26〜T28は既存期待結果を維持する。

## 合格基準

- T01のみが、全ゲートPASS後に削除を許可する。
- T02〜T20は、S6の条件を満たさない限り本体DELETEが一度も発行されないことをSQL traceまたはDELETE入口の呼出し監査で確認する。
- T11では2テーブルを単一トランザクションとして扱い、片方だけの確定を認めない。
- T16/T17では、DELETE commit済みか否かを正確に分離し、VACUUMや事後検査の失敗を削除成功の証明に流用しない。
- 全T01〜T28で共通証跡schemaの必須keyを記録し、証跡欠落・不整合はテストFAILとする。
- 失敗ログが残っていても、archiveや復元先のデータ完全性が確認できない場合は復元可能と判定しない。
- テスト実行後、本番DB・既存Artifact・Issue #31・正式な `docs/status/STATUS.md` に変更がないことを確認する。

## テスト実装時の注意

- SQLite一時DBとtmp filesystemのみを使用する。
- 容量不足・中断・破損は注入可能なfilesystem/connection adapterで再現する。
- テストのDELETE SQL traceを記録し、S4/S5 PASS前にDELETE文が発行されていないことを検証する。
- 例外を意図的に握りつぶす実装を検知するため、状態遷移とreturn statusの両方を検証する。
- 既存のGen675/Gen676監査Artifactは読み取り専用で参照し、変更・削除しない。

## 再レビュー追補テスト T21〜T28（2026-11-24）

| ID | ケース | fixture・注入条件 | 期待結果 | 本体削除可否 | 個別証跡 |
|---|---|---|---|---|---|
| T21 | commit直後停止・外部完了ログ失敗 | 一時DBにhistory候補PK=[101,102,103]の3行、lap候補PK=[201,202]の2行、保護root ID=[R001〜R037]の37行、receipt表を作成。全gate PASS後DELETE transactionをcommitし、DELETE_COMMITTED receipt更新・外部完了ledger追記直前でkill。別ケースでCOMMIT前kill/rollback | commit後停止はreceipt=COMMIT_PENDING、両表候補PK=0、root37/37一致、ledger=DELETE_INTENTのみとなりread-only recoveryでcommit成立を確定。commit前停止はreceipt=PREPARED、候補3/3・2/2が元digestで存在しNOT_COMMITTED。いずれも同一IDでDELETE再実行なし | recovery中禁止。同一ID永久禁止 | receipt状態、PK/row digest、external ledger、kill位置、DELETE trace各表1回、recovery判定 |
| T22 | 外部監査ログ書き込み失敗 | DELETE開始意図の外部永続ログ書込み/flush/fsyncを失敗させる | DELETE開始前にfail closed。DB本体無変更 | 禁止 | ログ書込例外、永続化確認結果、DELETE SQL trace |
| T23 | WAL/rollback-journal snapshot・同時更新・checkpoint | journal_mode=WALとjournal_mode=DELETEの一時DBをそれぞれ作成し、history PK=[101,102,103]の3行、lap PK=[201,202]の2行、root ID=[R001〜R037]の37行を用意。preview後に別接続で候補1行更新、1行追加、1行削除を個別注入。WALではsnapshot中writer更新/checkpointも注入し、DELETE journalでは比較snapshotのread-only接続が残った場合にCOMMITが阻害されること、およびDELETE前にsource/destination接続をcloseした場合にCOMMITできることを確認する。 | preview中metadata変化はsnapshot破棄。preview後の変更はBEGIN IMMEDIATE後のprojection/PK/row digest不一致で拒否。writer lock中の別writerはbusy timeout/lock拒否。rollback-journal modeでは比較read-only接続が残るclose失敗をfail closedで拒否し、DELETE SQL=0。正常時のCOMMIT成功はT01で両journal modeを検証する。 | 禁止 | identity、main/WAL stat+SHA、journal_mode、snapshot SHA、projection digest、PK/row差分、checkpoint/lock結果、read connection close証跡、DELETE trace |
| T24 | nonce期限・取消・MFA鮮度・再利用 | OIDC fixtureをiss=https://idp.example.test、sub=operator-001、aud=autoraceai-cleanup、session_id=sess-001、acr/amrは当該issuerのallowlistに登録したMFA profile、auth_time=t0に固定し、256-bit nonceを発行。nonceの10分期限直前/直後、auth_timeの300秒境界直前/直後・未来方向60秒境界、許可外acr/amr、step-upなし、取消後、CONSUMED後、同一/別operation_id再送、plan_digest変更、nonce ledger fsync失敗を注入 | nonceが10分以内、かつauth_timeが承認時刻から300秒以内・未来方向60秒以内、issuer別acr/amr allowlist一致、step-up再認証済み、nonce未使用/未取消の場合のみ承認可能。期限切れ・古いauth_time・未来時刻超過・許可外claim・取消・CONSUMED・別ID再送・計画変更・ledger fsync失敗は拒否。ledger状態は再起動後も維持 | 不正承認では禁止 | OIDC署名/claim検証、acr/amr profile、auth_time差分、step-up証跡、nonce_digest、発行/期限/取消/消費時刻、ledger連番/fsync、拒否理由 |
| T25 | 型保持canonical row digest | NULL、空文字、BLOB X'4142'、TEXT、INTEGER 1、REAL 1.0、REAL -0.0入力（SQLite readback確認）、日本語UTF-8、境界長fixtureをarchive/restore | canonical frameを次で固定する。row frameは固定magic文字列 ROWv1、table名のUTF-8 byte長(u32 big-endian)とbytes、column数(u32)、各column名のbyte長(u32)とbytes、storage-class tag(1 byte)、payload長(u64 big-endian)、payload bytesをcolumn順に連結する。tag/payloadはNULL=N/空payload、INTEGER=I/signed 64-bit big-endian、REAL=R/IEEE754 binary64 big-endian、TEXT=T/UTF-8の生bytes、BLOB=B/生bytes。TEXTはUnicode正規化しない。REALはSQLiteから読み戻したbinary64 bit patternを保持して符号化する。SQL bind時にSQLiteが-0.0を+0.0へ正規化した場合、そのreadback値をgoldenとし、canonicalizerが元の入力側bit patternを復元できるとは扱わない。各row frameをu64 length prefix付きでPK canonical frameのbyte辞書順に並べ、TABLEv1 magic・row count・各frameを連結してSHA-256を取る。PK digestもPKv1 magic・各PK frameを同じ順序で連結してSHA-256を取る。NULL/空文字、BLOB/TEXT、INTEGER/REALは別digest。+0.0/-0.0はSQLite readback後に異なるbinary64値として保持されている場合に限り別digestとなる。本fixtureではv_negzeroは+0.0 readbackとなることを期待する。fixture作成時にtypeof()を確認し、意図したstorage classでなければfixture自体をFAILとする。source/archive/restoreでPK/row digest完全一致。追加fixtureとして、PKなしテーブル（同一rowの重複を含む）と複合PKのWITHOUT ROWIDテーブルを作成し、入力順を変えてもdigestが一致すること、重複row数がdigestに反映されることを確認する。PK列にNULLを許すテーブルはPK順ではなくrow frame全体のbyte順へfallbackすることを検証する。 | 完全一致まで禁止 | typeof、tag、length、hex bytes、canonical byte SHA、PK/row digest、archive/restore比較 |
| T26 | schema/index/constraint変更 | preview後に列定義、PK、index、UNIQUE/CHECK/FK制約を変更またはarchive側から欠落 | schema fingerprint不一致を検知しarchive/restore gateまたは削除直前gateで拒否 | 禁止 | schema SQL、index/constraint一覧、期待/実測fingerprint |
| T27 | 同一operation_id並行実行 | 同一IDで複数worker/processから同時にpreview/approve/archive/deleteを開始 | 一意制約と排他lockにより一方だけが進行。他方は競合拒否。二重DELETEなし | 1 operationにつき最大1回、全gate後のみ | operation record、lock owner、競合ログ、DELETE SQL trace |
| T28 | 37 root保護条件 | Gen675由来37 rootの一部/全部をcandidateへ混入、保護行を変更/欠落させる | 通常整理をfail closed。archiveに存在しても本体保護条件を免除しない | 禁止 | root ID集合digest、保護状態、candidate混入件数、拒否ログ |
## 追加テストの合格基準

- T21〜T28はすべて失敗時にDELETEを禁止し、T21はcommit結果が確定するまで再DELETEを一切許可しない。
- T22では外部ログの永続化確認がない状態でDELETE SQLが0回であることをtraceで確認する。
- T23ではWALを含む一貫snapshotと削除直前の書き込みトランザクション内再照合を検証する。
- T25ではNULLと空文字、BLOBと文字列、整数と実数を混同しないcanonical serializationを仕様化してからテストする。
- T26ではテーブル列だけでなくPK、index、UNIQUE/CHECK/FK等のschema fingerprint対象を検証する。
- T27ではプロセスを跨ぐ排他を確認し、メモリ内lockだけに依存しない。
- T28では37 rootが1件でも候補に混入した場合に拒否し、archive保存の有無で例外を認めない。
