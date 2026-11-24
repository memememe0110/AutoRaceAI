# Issue #31 旧Ver整理のテスト計画（レビュー用）

- 状態: 実装前テスト計画
- 対象: 安全化設計S0〜S7
- すべての失敗ケースで、archive検証・別DB復元検証PASS前の本体DELETEは禁止。
- 証跡はoperation_idに紐付け、メインDB外にも保存する。

## テストケース

| ID | ケース | 入力・注入条件 | 期待結果 | 本体削除可否 | 必須証跡 |
|---|---|---|---|---|---|
| T01 | 正常系 | 2テーブルに既知件数の候補行と保護行。承認済み。archive/restoreの全検証を成功させる | S0→S7。件数・PK・行hash・SHA・integrity・復元が全一致。削除後も保護行が残る | S5まで禁止、S6条件成立後のみ許可 | preview manifest、approval、archive SHA、各検証結果、restore digest、削除前後件数、完了ログ |
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
| T16 | VACUUM失敗 | DELETE commitとS7データ検証完了後にVACUUMのみ失敗 | データ状態はDELETE_COMMITTED/POST_DELETE_VERIFIEDを保持し、保守状態のみPOST_DELETE_MAINTENANCE_FAILED。archive/restore/外部ログを保全し、再試行でDELETEしない | 新規DELETE禁止。保守処理のみ再試行可 | DELETE commit receipt、S7検証、vacuum_error、archive SHA、保全先、状態遷移 |
| T17 | 削除後検証失敗 | S6の削除commit後に残存件数/保護ID検査を意図的に失敗させる | SUCCESSにしない。archiveとrestore済みDBを保持し、復旧判断を要求 | 追加削除は禁止 | 削除commit receipt、post-check差分、archive SHA、復旧記録 |
| T18 | 未検証IDからDELETE | PREVIEWED/APPROVED/ARCHIVE_CREATED状態のoperation_idでDELETE APIを呼ぶ | API内部の状態・receipt gateで拒否 | 禁止 | 拒否されたoperation_id、状態、呼出元、gate判定 |
| T19 | 検証receipt改ざん | restore PASS記録、plan digest、archive SHA、状態値を改ざん | receipt署名/digest不一致で拒否 | 禁止 | receipt原本/実測、署名検証結果、拒否ログ |
| T20 | 削除直前の同時更新 | S5 PASS後、削除直前に対象行を追加・更新・削除 | 最終再スキャンでpreviewとの差異を検出し承認失効 | 禁止 | preview/current PK digest・row digest比較、DB identity |

## 合格基準

- T01のみが、全ゲートPASS後に削除を許可する。
- T02〜T20は、S6の条件を満たさない限り本体DELETEが一度も発行されないことをSQL traceまたはDELETE入口の呼出し監査で確認する。
- T11では2テーブルを単一トランザクションとして扱い、片方だけの確定を認めない。
- T16/T17では、DELETE commit済みか否かを正確に分離し、VACUUMや事後検査の失敗を削除成功の証明に流用しない。
- すべてのケースで、失敗状態・例外・operation_id・source identity・対象digest・archive情報が残る。
- 失敗ログが残っていても、archiveや復元先のデータ完全性が確認できない場合は復元可能と判定しない。
- テスト実行後、本番DB・既存Artifact・Issue #31・正式な `docs/status/STATUS.md` に変更がないことを確認する。

## テスト実装時の注意

- SQLite一時DBとtmp filesystemのみを使用する。
- 容量不足・中断・破損は注入可能なfilesystem/connection adapterで再現する。
- テストのDELETE SQL traceを記録し、S4/S5 PASS前にDELETE文が発行されていないことを検証する。
- 例外を意図的に握りつぶす実装を検知するため、状態遷移とreturn statusの両方を検証する。
- 既存のGen675/Gen676監査Artifactは読み取り専用で参照し、変更・削除しない。

## 一次レビュー追加テスト T21〜T28（2026-11-24）

| ID | ケース | 入力・注入条件 | 期待結果 | 本体削除可否 | 必須証跡 |
|---|---|---|---|---|---|
| T21 | DELETE commit直後のプロセス停止 | SQLite commit直後、外部完了ログ更新前にプロセスを強制停止 | 再起動後COMMIT_UNKNOWNとしてread-only照合。receipt・PK/row digest・外部ログから確定するまで停止 | 確定まで再DELETE禁止 | commit前後ログ、DB内receipt、再起動照合結果、状態遷移 |
| T22 | 外部監査ログ書き込み失敗 | DELETE開始意図の外部永続ログ書込み/flush/fsyncを失敗させる | DELETE開始前にfail closed。DB本体無変更 | 禁止 | ログ書込例外、永続化確認結果、DELETE SQL trace |
| T23 | WALを伴う同時更新 | WAL modeで別接続から対象行を更新/追加し、preview snapshot後に競合させる | snapshotと現在DBの差異またはロック競合を検知。承認失効し停止 | 禁止 | journal_mode、snapshot identity、WAL状態、PK/row digest差分、lock結果 |
| T24 | 承認nonce再利用 | 同じapproval_nonceを同一/別operation_idで2回使用、期限切れ・取消済みnonceも再送 | UNIQUE/使用済み台帳で拒否。再承認を要求 | 禁止 | nonce digest、認証主体、使用履歴、拒否理由 |
| T25 | 行digest型保持 | NULL、BLOB、整数/実数、文字列、境界値を含む行をarchive/restore | 型タグ・長さ・バイト列を含む正規化でsource/archive/restoreのPK・row digestが完全一致 | 一致確認まで禁止 | 列型、NULL区別、BLOB bytes、canonicalization仕様、各digest |
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

## T16差し替え（VACUUMの独立状態）

| ID | ケース | 入力・注入条件 | 期待結果 | 本体削除可否 | 必須証跡 |
|---|---|---|---|---|---|
| T16 | VACUUM失敗 | DELETE commitとS7データ検証が完了した後、VACUUMのみ失敗させる | データ状態はDELETE_COMMITTED/POST_DELETE_VERIFIEDとして保持し、保守状態のみPOST_DELETE_MAINTENANCE_FAILED。archive/restore/外部ログを保持し、VACUUM再試行でDELETEを再実行しない | 新規DELETE禁止。保守処理のみ再試行可 | DELETE commit receipt、S7検証、vacuum_error、archive SHA、保全先、状態遷移 |

- T16は削除処理の成否を判定するテストではなく、削除後保守処理の失敗分離を検証する。