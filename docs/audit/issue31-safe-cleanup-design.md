# Issue #31 旧Ver整理の安全化設計（レビュー用）

- 状態: 設計ドラフト。実装未着手。
- 対象: 予測履歴・周回スナップショットの旧バージョン整理処理
- 非対象: 予測ロジック、買い目、EV、回収率、APP_VERSION、本番DBの現時点の変更
- 絶対条件: 独立アーカイブの完全性検証と別DB復元確認がPASSする前に、本体DELETEへ到達できないこと。

## 1. 安全性の不変条件

1. 削除対象はプレビュー時点の主キー集合と行ハッシュで固定し、承認後に再計算した対象と一致しなければ失敗とする。
2. archiveファイルは本体DBと独立したSQLiteファイルとし、同一ファイル・同一DB内のarchive表を独立バックアップとみなさない。
3. archive検証と別DB復元検証の全項目がPASSするまで、本体DELETE APIは呼び出せない。
4. 検証済みoperation_idと結び付いた検証レシート以外ではDELETEを実行できない。
5. 削除直前に本体DBの識別情報、対象件数、主キー集合、行ハッシュを再照合する。プレビュー後に対象が変化していれば削除禁止。
6. 保護対象は候補集合から除外するだけでなく、独立した拒否条件として検査する。
7. 2テーブルの削除は一つのDBトランザクションにまとめ、片方だけの確定を成功扱いしない。
8. アーカイブは削除後検証・監査保持期間が終わるまで変更・削除・上書きしない。

## 2. operation_idと対象固定

operation_idは暗号学的に十分ランダムな一意IDとする。各operation_idに以下を固定する。

- source_db_identity: DB実体を識別するパスだけに依存しない識別情報。サイズ・SHA-256または安定したDB世代IDを含める。
- source_schema_fingerprint
- keep_versions / delete_versions
- 対象テーブル一覧
- 各テーブルの主キー列定義
- candidate_count
- candidate_primary_key_set_digest
- candidate_rows_digest（正規化した列値を行単位にハッシュし、主キー順に集約）
- preview_plan_digest
- operator_id / approved_at / approval_nonce

operation_idは作成後に別の対象集合へ再利用しない。同じoperation_idで異なる対象を指定した場合は拒否する。再試行は同じ計画・同じsource identityに対してのみ許可し、計画変更時は新しいoperation_idを発行する。

## 3. 状態遷移 S0〜S7

### S0: IDLE
- 有効な処理なし。
- 既存の完了・失敗operation_idを再利用しない。

### S1: PREVIEWED
- source DBを読み取り専用で開き、スキーマ・DB identity・対象バージョン・件数を取得。
- 削除候補の全主キー集合と全行ハッシュを算出。
- 保護対象の存在と候補混入を確認。
- preview manifestを永続保存し、plan_digestを確定。
- この段階では本体を一切変更しない。

### S2: APPROVED
- 操作者へ対象バージョン、テーブル別件数、保護対象件数、archive出力先、source identityを表示。
- 明示的な承認操作を要求。単なるページ表示や既定値選択は承認とみなさない。
- 承認はoperation_idとplan_digestに結び付ける。
- 承認後の対象変更・DB identity変更は承認失効とする。

### S3: ARCHIVE_CREATED
- 独立した一時ファイルへarchiveを作成。
- 既存archiveをunlink/上書きしない。新規一意ファイル名で作成し、完了後に検証済みの固定パスへatomic renameする。
- 元DBから対象行を読み、主キー・列値を保持して出力。列型、NULL、BLOB、インデックス、必要な制約を明示的に保持する。
- ファイルclose後に必要なflush/fsyncを行う。
- 失敗時は本体を変更せず、partial archiveを隔離して失敗ログを残す。

### S4: ARCHIVE_VERIFIED
以下をすべて検証する。1つでも不一致ならFAILED_ARCHIVE_VALIDATIONとし、先へ進めない。

- テーブル別の期待件数とarchive件数が完全一致。
- 主キー集合が完全一致（件数だけの一致では不可）。
- 主キー順に正規化した全行ハッシュが一致。
- archiveファイルをcloseした後のファイルSHA-256を計算・記録。
- archive SQLiteを新規接続し、各テーブルの存在・スキーマを検査。
- `PRAGMA integrity_check` が全DBで `ok`。
- 必要な外部キー・インデックス・制約の検査がPASS。
- archive検証manifestをoperation_idとplan_digestに結び付けて永続保存。

### S5: RESTORE_VERIFIED
- archiveから別の空SQLiteへ復元する。元DBにも本番DBにも復元しない。
- 復元先のintegrity_checkが `ok`。
- テーブル別件数、主キー集合、全行ハッシュが元の削除候補と完全一致。
- 37 rootを含む保護対象・復元対象の期待値を検査。
- 復元検証manifestとarchive SHA-256をoperation_idに結び付ける。
- 全項目PASSしなければFAILED_RESTORE_VALIDATION。

### S6: DELETE_AUTHORIZED / DELETE_COMMITTED
削除を許可する唯一の条件:
- operation_idがPREVIEWED→APPROVED→ARCHIVE_CREATED→ARCHIVE_VERIFIED→RESTORE_VERIFIEDの順で遷移済み。
- 各状態のmanifest/receiptが永続化され、署名または改ざん検出可能なdigestで結び付いている。
- 操作者承認のplan_digestと現在の計画が一致。
- source DB identityがプレビュー時と一致。
- 削除直前の対象主キー集合・行ハッシュがpreviewと一致。
- 保護対象の削除候補混入が0件。
- archiveファイルSHA-256がS4の値と一致し、archiveが読み取り可能。
- すべての条件を満たしたoperation_idに限りDELETEを許可。

削除APIは `delete_verified_operation(operation_id)` の一つに限定する。任意のtable/versionを受け取る汎用DELETE関数をUIや他の呼び出し元に公開しない。DELETE APIはoperation_idから永続レシートを読み、状態・plan_digest・source identity・archive hash・restore PASSを自分で再検証する。単なる `verified=True` の引数は認めない。検証レシートを発行するvalidator以外から作成できない内部capabilityも併用する。

削除は両テーブルを同一トランザクションで実行し、直前の主キー集合・行ハッシュを再確認する。どちらかのDELETEや件数照合が失敗したらrollbackし、DELETE_COMMITTEDへ進めない。

### S7: POST_DELETE_VERIFIED
- 削除後の本体件数・残存主キー・保護対象を再照合。
- 削除対象が本体から消え、archiveと復元検証済みコピーに全件残っていることを確認。
- 操作ログを完了状態にし、archiveを保全。
- 削除後検証が失敗した場合はSUCCESSにしない。archiveを保持し、失敗証跡を残し、復旧判断へ移す。自動で本番DBへ上書き復元しない。

## 4. 削除を構造的に遮断する実装要件

- DELETE SQLは単一の内部関数に隔離し、UI・worker・他モジュールから直接呼ばせない。
- DELETE関数の唯一の入口はoperation_id。テーブル名・versionリストを外部引数で受け取らない。
- 関数内部でoperation recordの状態、承認、archive SHA-256、archive validation、restore validation、plan_digest、source identityを検証する。
- 検証receiptはoperation_id、source identity、plan digest、archive SHA、検証結果、時刻を結合した改ざん検出可能な記録とする。
- operation recordが欠落・重複・不整合・途中状態・不明状態ならfail closed。
- 検証状態を手動で文字列変更するだけでは削除権限を得られないよう、receiptのdigest/HMACまたは同等の検証を行う。
- 削除直前に本体DBを再スキャンし、対象集合が変わっていたら承認と検証を無効化する。
- 本番実装でPython内部関数の命名規約だけをセキュリティ境界とみなさない。SQLite接続・DELETE実行権限を一つのサービス境界に集約する。

## 5. 操作ログと失敗時証跡

ログはメインDBとは独立した永続先にも保存する。最低限の項目:

- operation_id、状態遷移、開始/終了時刻
- operator_id、承認時刻、approval_nonce
- source DB identity、schema fingerprint
- keep_versions、delete_versions、plan_digest
- テーブル別候補件数、主キー集合digest、行digest
- archive path、archive size、archive SHA-256
- integrity_check結果、復元先identity、復元結果
- 削除開始・commit結果、削除後件数
- 各検証のPASS/FAIL、エラー型・メッセージ・traceback
- retry回数、親operation_id、実行環境識別情報

失敗時は直前状態を保持し、エラーを握りつぶしてokにしない。アーカイブ作成済みならpartial/failedファイルを隔離し、削除しない。既存の正常archiveを上書きしない。部分成功は全体FAILとし、どのテーブル・段階まで進んだかを記録する。

## 6. リトライ・重複実行

- operation_idにUNIQUE制約を設ける。
- 同じoperation_idの同時実行はロックし、二重開始を拒否する。
- 完了済みoperation_idの再実行は冪等な結果参照のみ許可し、再DELETEしない。
- 失敗後の再開は最後に永続化された状態から行う。ただしsource identityまたはplan digestが変化した場合は再開禁止、新operation_idを要求する。
- S3〜S5の再実行では新しい一時archiveを作成し、既存の検証済みarchiveを保持する。
- DELETE commit後に通信断等で結果不明の場合は、まずDB状態とoperation logをread-onlyで照合し、推測でDELETEを再実行しない。

## 7. 実装前レビュー項目

- [ ] DELETE SQLが単一入口に隔離されている。
- [ ] 検証PASS前のDELETE呼び出し経路が存在しない。
- [ ] operation_idとplan_digestで対象が固定される。
- [ ] 承認後の対象変更で必ず拒否される。
- [ ] archiveの件数・主キー・行ハッシュ・SHA-256・integrity_checkを検査する。
- [ ] 別DB復元の全項目PASSを要求する。
- [ ] 重要履歴の保護を独立検査する。
- [ ] 失敗・中断・部分成功を成功扱いしない。
- [ ] archiveと操作ログが本体DBとは独立して保全される。
