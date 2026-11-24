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
| T16 | VACUUM失敗 | 削除commit後のVACUUMだけを失敗させる | データ削除の成否とVACUUM成否を分離。VACUUM失敗を隠して全体成功にしない | S6前は禁止。S6後のVACUUM失敗は削除結果を偽装せず、S7未完了/要対応 | DELETE commit結果、vacuum_error、DB integrity、操作状態 |
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
