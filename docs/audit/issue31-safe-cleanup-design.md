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

## 一次設計レビュー指摘への必須追補（2026-11-24）

**レビュー判定：要修正。設計承認未了。以下は既存本文に優先する規範要件であり、実装済みを意味しない。**


### R1. SQLite識別・固定スナップショット取得（規範仕様）

#### R1-1. 接続と取得順
1. 対象DBをrealpathで絶対パス化する。最初に専用coordinator接続をread-writeで開き、busy_timeout=5000msを設定してBEGIN IMMEDIATEを取得する。この接続ではデータ変更を一切行わず、snapshot取得中の外部writerを排除する。取得できなければpreviewを開始しない。
2. coordinator lock取得後、別の読み取り専用URI mode=ro接続を開く。immutable=1は指定せず、query_only=ONを設定してBEGINし、schema_version、journal_mode、sqlite_schemaを読む。読み取りtransactionはBackup API完了とmetadata採取まで維持する。
3. Backup API開始直前にmain DBと存在するWALのdevice、inode、size、mtime_ns、SHA-256を採取する。WAL不存在も明示記録する。
4. 読み取り専用接続をsource、専用一時SQLite接続をdestinationとしてSQLite Backup APIで全DBをコピーする。sourceとdestinationは別connectionとする。Backup APIは複数stepで進行し得るため、coordinatorのBEGIN IMMEDIATEを全step完了まで保持してsource更新を防ぐ。排他的な新規一意pathを使い、既存fileをunlink/上書きしない。Backup APIの非成功、busy/locked timeout、read error、中断は失敗とする。
5. Backup完了後、出力接続でjournal_mode=DELETE、synchronous=FULLを設定しcommit・closeする。snapshot本体と親directoryをfsyncする。読み取り元transactionを維持したままmain/WAL metadataとSHA-256を再採取する。
6. 取得前後metadata採取が終わった後、読み取り元transactionをROLLBACKしてcloseし、coordinator transactionもROLLBACKしてwriter lockを解放する。snapshot検証はclose済みの一時ファイルを別のread-only接続で行う。
7. 取得前後のmain/WAL device、inode、size、mtime_ns、SHA-256、journal_mode、schema_versionのいずれかが変化した場合、snapshotを破棄し、そのoperation_idを終端化する。再取得は新しいpreviewと新しいoperation_idで行う。
8. 検証順を固定する。①snapshot file SHA-256/size、②integrity_check、③schema fingerprintと対象table定義、④対象2表PK集合digest、⑤対象2表全行digest、⑥37 root digest、⑦source_db_identityとplan_digest。全PASS後にPREVIEWEDを永続化する。
9. PK/row digestはsnapshot DBから読み取り専用で計算し、元DBの別接続から値を混在させない。

#### R1-2. WAL・checkpoint・transaction
- WAL modeではBackup APIが読み取りtransactionから見える確定状態をコピーする。main DBファイル単体コピーは禁止。
- preview取得中はcoordinator接続のBEGIN IMMEDIATEを維持し、外部writerを排除する。手動checkpointは実行しない。WAL checkpointは別処理として動作し得るため、外部checkpointが発生した場合は取得前後metadata差異として検出しoperationを破棄する。
- 削除時はR4のBEGIN IMMEDIATEでwriterを確保する。削除直前比較snapshotは別read-only接続からBackup APIで取得し、writer transaction保持中の対象DBと比較する。比較中にsource metadataが変われば削除禁止。
- WAL欠落、読取不能、journal_modeと関連file状態の矛盾、checkpoint状態を判別できない場合はfail closed。WALの手動削除・無視・強制checkpointは禁止。

#### R1-3. source_db_identityとsnapshot digest
source_db_identityは次の値をキー順固定・空白なしUTF-8 JSONへ正規化しSHA-256を取る。
- canonical_db_path（realpath）
- filesystem_device、filesystem_inode（main DB）
- sqlite_application_id、sqlite_user_version、sqlite_page_size、sqlite_encoding
- schema_version、journal_mode
- schema_fingerprint（receipt管理表を含む全sqlite_schema定義）
このidentityにはsize、mtime、main DB SHA、WAL SHAを混ぜない。これらはsnapshot_capture_evidenceとして別途保存し、取得前後一致を必須とする。
- source_snapshot_sha256はBackup API snapshot file全体のSHA-256であり、監査証跡として不変保存する。
- receipt管理表のPREPARED行・状態更新はsnapshot取得後に行うため、DB file全体のSHA-256はその更新で変化し得る。したがって削除直前の論理一致判定にfile SHAを直接使わず、source_db_identity、schema_fingerprint、source_projection_digest、対象digestの一致で判定する。source_snapshot_sha256は承認時snapshotの固定証拠として保持し、再計算値で上書きしない。
- source_projection_digestはreceipt管理表を除く全通常ユーザーテーブルのschema定義と全行を、次の固定順で型付き正規化して集約したSHA-256とする。table一覧はUTF-8 byte辞書順、列は `PRAGMA table_xinfo` のcid順、schema定義はsqlite_schemaのtype/name/tbl_name/sqlをUTF-8 byte辞書順で固定する。各rowはcanonical row frameを作成し、PKが全列NOT NULLかつ一意である場合はPK frame順、そうでない場合またはPKがない場合はrow frame全体のbyte辞書順で整列する。同一row frameは重複数を保持する。複合PKはPRAGMA table_xinfoのpk ordinal順に各列のname/tag/payloadをframe化する。WITHOUT ROWIDも同じ規則を使う。virtual tableはshadow tableを含む完全な論理内容のcanonicalization方式が別途定義されるまで対象DBとして拒否する。target 2表はPKが一意・NOT NULLでない場合、対象集合を安全に固定できないため拒否する。receipt管理表以外を除外してはならない。
- 削除直前はsource_db_identity、schema_fingerprint、source_projection_digest、対象2表のPK/row digest、37 root digestを承認時と照合し、1つでも異なればDELETE禁止。
- receipt管理表は事前作成しschema_fingerprintに含める。可変receipt行のみsource_projection_digestから除外する。他の除外table/columnは認めない。

### R2. 承認真正性・nonce（規範仕様）

#### R2-1. 操作者認証
- 承認主体は許可リスト登録済みOpenID Connect Identity Providerが発行する署名検証済みID tokenのissuer（iss）とsubject（sub）の組で識別する。表示名・メール・ブラウザ入力文字列だけでは識別しない。
- tokenの署名、許可issuer、audience、期限、発行時刻、auth_timeを検証する。承認時はMFA必須とし、IdPごとに事前登録したacr許可値とamr許可組合せの両方を満たさなければ拒否する。単一のamr値を全IdP共通でMFAとみなしてはならない。認証基盤が利用できない場合は承認/DELETE機能を無効化する。
- 承認時はstep-up再認証を要求し、OIDC max_age=300秒相当を適用する。検証済みauth_timeは承認時刻から300秒以内かつ未来方向60秒以内でなければならない。境界判定は `0 <= approval_time - auth_time <= 300秒` を基本とし、許容clock skewは未来方向60秒のみとする。IdPがこの鮮度を保証できない場合は承認不可とする。
- issuer、subject、session_id、auth_time、acr、amr、ID token SHA-256、承認時刻を保存する。生token・秘密鍵・cookieは保存しない。

#### R2-2. nonce・期限・台帳
- approval_nonceはOS CSPRNGで生成する256-bit乱数のbase64url表現。有効期限は発行から10分に固定し、延長・再利用・同一nonce復活は禁止。
- nonce台帳はmain DB外の追記専用ledgerとする。プロセス間排他lock下で追記し、各event後にfsync、初回作成時は親directoryもfsyncする。途中行・破損・読取不能・連番欠落時は承認/DELETEを停止する。
- nonce_digestを一意キーとしISSUED、CONSUMED、CANCELLED、EXPIREDを記録する。ISSUEDは発行時、CANCELLEDは取消時、CONSUMEDはDELETE_INTENT記録直前に永続化する。期限到達後はEXPIREDを記録する。
- 取消は認証済み操作者が専用操作を行い、CANCELLEDをfsyncした時点で成立。取消後は同じnonce/operation_idを再承認に使わない。新しいpreview、operation_id、nonceが必要。
- CONSUMED/CANCELLED/EXPIRED nonceは同一・別operation_idを問わず永久に再利用禁止。ledgerに存在するnonce_digestは必ず拒否。

#### R2-3. 承認レコード
- 承認レコードはoperation_id、plan_digest、source_db_identity、source_snapshot_sha256、source_projection_digest、archive destination、approval_nonce_digest、issuer、subject、session_id、auth_time、承認時刻、有効期限を一体固定する。
- 正規化レコードをmain DB外の秘密管理領域の鍵によるHMAC-SHA-256で署名する。鍵読取不能、署名不一致、鍵ID不明は拒否。
- operation_idはUUIDv4相当CSPRNGで一度だけ発行し永続台帳でUNIQUE。nonceもUNIQUE。operation_id、plan_digest、source_db_identity、nonce_digestを同じ承認recordに結合する。
- 承認後に対象一覧、件数、keep/delete versions、DB identity、schema、source projection、対象digest、archive destination/SHA、restore receiptのいずれかが変われば承認失効。変更後の計画へ同じoperation_idを流用しない。

### R3. 外部ログ・DB内receipt・COMMIT_UNKNOWN（規範仕様）

#### R3-1. DB内receipt必須schema
対象DBに専用receipt管理表を事前作成する。必須列:
- operation_id TEXT PRIMARY KEY
- plan_digest TEXT NOT NULL
- source_db_identity TEXT NOT NULL
- source_projection_digest TEXT NOT NULL
- target_pk_digest_history TEXT NOT NULL
- target_row_digest_history TEXT NOT NULL
- target_pk_digest_lap TEXT NOT NULL
- target_row_digest_lap TEXT NOT NULL
- archive_sha256 TEXT NOT NULL
- restore_receipt_sha256 TEXT NOT NULL
- state TEXT NOT NULL（PREPARED / COMMIT_PENDING / DELETE_COMMITTED / NOT_COMMITTED / MANUAL_RECOVERY_REQUIRED）
- created_at_utc TEXT NOT NULL
- updated_at_utc TEXT NOT NULL
- receipt_digest TEXT NOT NULL
- receipt_key_id TEXT NOT NULL
- receipt_hmac TEXT NOT NULL
operation_idはPRIMARY KEY。receipt_digestはreceipt_hmacを除く全receipt列を固定順・型付きcanonical encodingで正規化したSHA-256とする。receipt_hmacはreceipt_digest、receipt_key_idおよびreceiptの全フィールド（receipt_hmac自身を除く）を含むcanonical recordに対するHMAC-SHA-256とする。鍵はmain DB外のsecret managerに保管し、DB書込権限だけでは再計算できないこと。key_idごとの検証鍵は当該receiptの保全期間中保持し、rotation後も旧key_idを検証専用で残す。未知key_id、鍵読取不能、HMAC不一致はMANUAL_RECOVERY_REQUIREDとしてfail closed。receipt表schemaはschema_fingerprintに含め、可変receipt行だけsource_projection_digestから除外する。

#### R3-2. receipt・外部ログ順序
1. archive/restore PASS、承認有効、nonce未使用を確認後、DB内receiptをPREPAREDとして別transactionでINSERT/commitする。receipt_digestとreceipt_hmacを生成し、commit後に両方を再検証する。
2. nonce ledgerへCONSUMEDを追記しfsyncする。失敗ならDELETE transactionを開始しない。receiptはPREPAREDのまま保持し、同一operation_idを再実行しない。
3. main DB外append-only ledgerへDELETE_INTENTを追記しfsyncする。内容はoperation_id、plan_digest、source_db_identity、source_projection_digest、archive SHA、restore receipt SHA、承認証跡digest、nonce_digest、時刻。失敗時はDELETE transactionを開始しない。CONSUMED nonceは取消済み相当として永久に再利用しない。
4. R4の書き込みtransaction内で再照合後、2表DELETEと件数照合を行う。receiptをCOMMIT_PENDINGへ更新しreceipt_digestとreceipt_hmacを再計算する。このUPDATEはDELETEと同一transaction内でCOMMIT直前に行う。
5. SQLite COMMIT成功後、別の短いtransactionでreceiptをDELETE_COMMITTEDへ更新し、receipt_digestとreceipt_hmacを再計算してcommitする。その後、外部ledgerへDELETE_COMMITTEDを追記しfsyncする。後段receipt更新または外部ログ追記が失敗した場合はCOMMIT_UNKNOWNとし、DELETE再実行禁止。

#### R3-3. COMMIT_UNKNOWN復旧判定表
再起動後の初動は対象DBをmode=ro、query_only=ONで開き、receipt HMAC/key_idの検証、対象PK、対象row digest、保護root digest、schema、外部ledgerを読むだけとする。receipt HMACが不一致・検証不能なら、PKの有無にかかわらずMANUAL_RECOVERY_REQUIREDとする。判定確定までDELETE、VACUUM、archive削除、operation_id再利用を禁止する。

| DB receipt | 対象2表 | 保護対象/schema | 外部ledger | 判定 |
|---|---|---|---|---|
| COMMIT_PENDING | 両表の全候補PKが不存在 | 承認時digestと一致 | DELETE_INTENTあり、完了記録なし | commit成立と確定。DELETE_COMMITTEDへ復旧記録 |
| DELETE_COMMITTED | 両表の全候補PKが不存在 | 承認時digestと一致 | DELETE_INTENTあり。DELETE_COMMITTED完了記録は存在しても未記録でもよい | commit成立済み。完了記録がなければreceipt/ledgerだけ補完 |
| PREPARED | 両表の全候補PKが全件存在し、全row digest一致 | 承認時digestと一致 | ledger読取可能、DELETE_INTENTあり、完了記録なし | NOT_COMMITTED確定。operation終端化、新operation_idでのみ再preview |
| PREPARED | 両表の全候補PKが全件存在し、全row digest一致 | 承認時digestと一致 | ledger読取可能、DELETE_INTENTなし | DELETE transaction未開始と確定。NOT_COMMITTEDで終端化し、新operation_idでのみ再preview |
| 上記以外 | 一部だけ存在、digest不一致、receipt欠落/破損 | 不一致または判定不能 | 欠落・破損・不一致 | MANUAL_RECOVERY_REQUIRED。人手判断まで停止 |

- commit後の外部ledger追記失敗時、receiptはCOMMIT_PENDINGまたはDELETE_COMMITTED、候補PKは全件不存在となる。表の全条件を満たす場合だけcommit成立と判定し、DELETEせずreceipt/ledger完了記録だけを補完する。
- PREPAREDかつ全対象行が元digestで存在し、receipt HMACと外部ledgerが正常に検証できる場合は、DELETE_INTENTの有無にかかわらずNOT_COMMITTED。同じoperation_idでDELETEを再試行しない。nonceがISSUEDならCANCELLEDを追記しfsync、CONSUMEDなら使用済み状態を維持する。新operation_idでpreview・承認・archive・restoreを最初から行う。ledgerまたはHMAC鍵が読めない場合はMANUAL_RECOVERY_REQUIRED。
- receipt/PK状態の矛盾、一部だけ消失、外部ledger欠落/破損時は自動判定せずMANUAL_RECOVERY_REQUIRED。
- recovery初動はread-only照合のみ。DB更新は判定確定後の専用recovery操作に限定し、その操作にDELETE SQLを含めない。

### R4. 排他方式・削除transaction順（規範仕様）

#### R4-1. 採用方式
SQLite排他方式はBEGIN IMMEDIATEに固定する。BEGIN、EXCLUSIVE、メモリ内lockへの切替は認めない。busy_timeout=5000msとし、5秒以内にwriter lockを取得できなければ拒否する。プロセス間補助lockは同一operation二重開始防止にのみ使い、DB排他の代替にしない。

#### R4-2. 固定処理順
1. 承認、nonce、archive SHA、restore receipt、外部DELETE_INTENT fsyncを確認。
2. 対象DBへ書き込み接続しbusy_timeout=5000msを設定、BEGIN IMMEDIATE。取得失敗ならDELETEせずoperationをLOCK_REJECTEDとして終端化。
3. lock保持中にsource_db_identity、schema_fingerprint、source_projection_digest、2表の対象PK/row digest、37 rootの存在・件数・PK/row digestを再計算。
4. 別read-only接続でBackup API比較snapshotを取得し、取得前後のmain/WAL metadata不変を確認する。比較snapshotの検証とmetadata採取が完了したら、source read-only接続をROLLBACKしてcloseし、destination接続もcloseする。これらのclose完了を確認する前にDELETE SQLを発行してはならない。close失敗・接続残存・metadata不一致、identity/schema/projection/対象digest不一致のいずれかがあればDELETE禁止。receiptはPREPAREDのまま、承認・archive/restore receiptを失効。
5. 37 rootが全件本体に存在し、承認時PK/row digestと一致し、通常candidate混入0件を確認。1件でも欠落・変更・混入ならDELETE禁止。
6. historyとlap snapshotの2表を同一BEGIN IMMEDIATE transaction内でDELETE。各表の削除件数は承認済みcandidate_countと完全一致させる。
7. 2表DELETE成功後、DB receiptをCOMMIT_PENDINGへ更新しreceipt_digestを再計算。receipt更新/digest照合失敗なら両表DELETEをROLLBACK。
8. DELETE SQL trace、削除件数、receipt更新をtransaction内で検査しCOMMITを1回だけ実行。commit応答喪失・停止・接続断はCOMMIT_UNKNOWN。
9. commit前の例外、件数不一致、schema/digest不一致、root不一致、busy/locked timeoutはROLLBACK。同一operation_idのDELETE再試行は禁止し、新previewとoperation_idを要求。

#### R4-3. 再試行・拒否条件
- BEGIN IMMEDIATEのbusy/locked timeoutは自動再試行しない。operationを拒否し、待ち時間とSQLite error codeを記録。
- snapshot/schema/identity/PK/row/root digest不一致は承認を自動更新しない。承認・receiptを失効し新operation_idを要求。
- 片側DELETE失敗、件数不一致、receipt更新失敗、constraint違反はtransaction全体をROLLBACK。
- COMMIT_UNKNOWN、MANUAL_RECOVERY_REQUIRED、DELETE_COMMITTED、NOT_COMMITTED、LOCK_REJECTEDでは同一operation_idのDELETE入口を永久に再開しない。

### R5. 37 rootの保護ポリシー
- Gen675由来の37 rootは通常の旧Ver整理から除外する恒久保護対象として定義する。候補から除外するだけでなく、DELETE直前の独立した拒否条件として照合する。
- 保護対象として本体DBに存在する場合、通常archive対象・通常DELETE対象に含めない。保護対象の存在・件数・PK digestをpreviewと削除直前に照合し、予期せぬ欠落・変更も停止条件とする。
- アーカイブへ保存する要件は本体に残す保護要件とは別管理とする。archive/restore検証で保護対象がコピーされていても、本体DELETEを許可する根拠にはならない。
- 将来、保護対象37 rootを本体から削除する必要が生じた場合は、通常の承認とは別の追加承認を必須とする。追加承認には対象rootの全ID・理由・復元計画・影響を表示し、独立した認証済み承認nonceを要求する。今回の通常整理設計では追加承認が存在しても37 root削除を許可せず、別設計・別レビューを要求する。
- 保護対象の候補混入、PK集合不一致、保護フラグ不整合は1件でも検出したらfail closed。

### R6. VACUUMと削除結果の分離
- VACUUMは削除安全性・archive完全性・復元可能性の証明条件から分離し、DELETE commitおよびS7データ検証の後に行う任意の保守処理とする。
- VACUUM失敗はPOST_DELETE_MAINTENANCE_FAILEDとして記録し、DELETE_COMMITTED/POST_DELETE_VERIFIEDの結果と別フィールドで保持する。VACUUM失敗をデータ削除失敗や削除未実行と誤表示しない。
- VACUUM失敗時もarchive、復元検証済みDB、SHA-256、PK/row digest、外部操作ログを保持し、保全期間中の削除・上書きを禁止する。
- VACUUM再試行は同じoperation_idのデータDELETEを再実行せず、保守処理だけを再試行する。VACUUMの成否にかかわらず削除結果は独立照合可能でなければならない。

### 状態遷移への追加
- COMMIT_UNKNOWN、MANUAL_RECOVERY_REQUIRED、POST_DELETE_MAINTENANCE_FAILEDを追加状態/結果として定義する。
- いずれの状態もDELETE再実行を許可しない。COMMIT_UNKNOWNはread-only照合のみ、MANUAL_RECOVERY_REQUIREDは人手の復旧判断のみ、POST_DELETE_MAINTENANCE_FAILEDはデータ削除結果を保持したまま保守再試行のみを許可する。

### 設計承認ゲート
- R1〜R6の各項目について、具体的な状態遷移、データ項目、失敗時挙動、テストIDへの対応が記載されるまで設計承認しない。
- 本追補は設計要件であり、実装済み・検証済みを意味しない。実装・本番コード・DB・予測ロジック・APP_VERSIONは変更しない。