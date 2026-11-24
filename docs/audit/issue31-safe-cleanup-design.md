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

### R1. SQLite識別・WAL・一貫スナップショット
- プレビューはSQLite Backup API等で整合した読み取りスナップショットを別ファイルへ取得してから行う。WAL利用時にメインDBファイル単体をコピーしてスナップショットとみなしてはならない。読み取り接続で確定したトランザクション境界を明示し、必要なWAL内容を含む一貫状態を取得する。
- スナップショットに対し、DBファイルSHA-256、サイズ、生成時刻、SQLiteバージョン、journal_mode、schema fingerprint、対象テーブル定義、PK集合digest、全対象行digest、保護対象digestを記録する。DB identityはパスやファイルサイズだけで定義しない。
- plan_digestは上記identity、schema、keep/delete versions、対象テーブル、PK/row digest、保護対象条件、archive仕様を正規化して算出する。
- 削除直前は書き込みトランザクション内で現在DBのschema・対象PK集合・行digest・保護対象を再照合する。プレビューsnapshotとの差異、WAL checkpoint/接続状態の不整合、identity不一致、読み取り不能、競合更新のいずれかがあればfail closedし、承認・archive/restore receiptを失効させてDELETE禁止とする。
- WALを含む現在状態を安全に同定できない環境では削除機能自体を無効化する。WALファイルを手作業で削除・無視してはならない。

### R2. 承認の真正性
- 承認は認証済みセッションの操作者が、対象一覧・件数・保持/削除Ver・DB identity・archive先・plan_digestを表示した専用確認画面で明示的に実行する。単なるログイン、画面表示、チェックボックス初期値は承認ではない。
- 承認レコードはoperation_id、plan_digest、認証主体ID、認証セッション/認証強度の証跡、承認時刻、期限、暗号学的乱数approval_nonceを一体で固定し、改ざん検出可能な署名/HMAC等で保護する。
- approval_nonceは一回限りでUNIQUE管理し、使用済み・取消済み・期限切れnonceの再利用を拒否する。期限は設計値として実装前に明示し、期限切れなら再プレビュー・再承認を要求する。
- 承認取消はDELETE開始前まで可能。取消後は承認状態を復活させず、新しい承認nonceを要求する。
- operation_id、plan_digest、source DB identity、schema、PK/row digest、keep/delete versions、archive destinationのいずれかが変われば承認は即時失効し、同一operation_idでの差し替えを禁止する。

### R3. 外部監査ログとDB commitの不確定窓
- DELETEトランザクション開始前に、メインDBとは独立した永続ログへoperation_id、plan_digest、source identity、archive SHA、restore PASS receipt、承認情報、DELETE開始意図を追記し、flush/fsync等の永続化確認を得る。外部ログに書けない場合はDELETE開始禁止。
- 外部ログとSQLite commitは原子的に同時確定できないため、DELETE開始記録後からcommit結果確定までの状態としてCOMMIT_UNKNOWNを定義する。
- commit応答喪失・プロセス停止時は、再起動後に本体DBを読み取り専用で開き、operation receipt、対象PKの存在/不存在、対象行digest、保護対象、外部ログを照合する。推測で成功・失敗を決めない。
- COMMIT_UNKNOWN中は再DELETE、VACUUM、archive破棄、operation_id再利用を禁止する。整合が確定した場合のみCOMMITTEDまたはNOT_COMMITTEDへ遷移し、矛盾・ログ欠落時はMANUAL_RECOVERY_REQUIREDで停止する。
- DELETE後のcommit結果を証明するため、同一DBトランザクション内にoperation_idと削除結果digestを記録する内部receipt行を設ける案を必須検討とする。ただし外部ログとの不一致は別途照合し、DB内receiptだけで外部ログ永続性を代替しない。

### R4. 削除直前の競合防止と原子性
- DELETE処理は対象DBに対する書き込みトランザクションを開始してから、source identity/schema、対象2表のPK集合・全行digest、保護対象37 rootの不存在/保護状態を再照合する。SQLiteでは競合を早期検知できるトランザクション開始方式（例: BEGIN IMMEDIATE）を採用するか、同等の排他保証を設計書で具体化する。
- 再照合から2テーブルDELETE・削除件数照合・operation receipt書込みまでを同一トランザクション内で実施する。再照合後に別接続が対象行を変更できる競合窓を残さない。
- どちらか一方のDELETE件数不一致、SQL例外、receipt書込み失敗、制約違反があれば全体rollback。部分commitは認めない。
- トランザクション開始不能、busy/locked timeout、WAL状態不明、schema変更、PK/row digest不一致の場合はDELETEせず、承認・検証を失効させて再プレビューへ戻す。

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