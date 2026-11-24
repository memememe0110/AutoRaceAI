# Issue #31 現行アーカイブ処理の監査仕様

- 対象ソースコミット: `ea9be46d094ae2c6b4a139919ecc34a5819de40f`
- 対象ファイル: `app.py`
- app.py blob SHA: `ce637255569886f4c59a493f09964819ec7d2f00`
- 調査方法: 対象コミットのGit blobを取得し、ソース行番号を1始まりで再計数
- 注意: 本文はコード上の挙動を記録する。2026-11-09の実操作を直接証明するものではない。

## 1. 関数と処理順

### 1.1 `_v319_archive_old_version_predictions()`

定義: `app.py:36393-36471`

1. `36395-36401`: `keep_versions` が指定されない場合、`APP_VERSION`、`_V231_APP_VERSION`、Ver319、Ver322〜Ver326を既定保持候補とする。引数が指定された場合はその値を使い、空文字を除外してset化する。
2. `36402-36405`: reportを初期化し、SQLiteへ接続。既存テーブル名を取得する。
3. `36407-36411`: `v319_prediction_history_archive` がなければ、`v231_prediction_history` の0行SELECTから同形の空アーカイブ表を作る。
4. `36412-36416`: 本体履歴テーブルと `app_version` 列が存在する場合、保持外判定用のSQLプレースホルダーを組み立てる。
5. `36417-36427`: `COALESCE(app_version,'') NOT IN (keep_versions)` かつ、同じ `history_id` がアーカイブ表に存在しない行をアーカイブ表へINSERTする。
6. `36428-36436`: 同じ保持外条件で `v231_prediction_history` をDELETEし、INSERT件数とDELETE件数をreportへ記録する。両件数が一致することを検証する処理はない。
7. `36437-36444`: `v252_lap_prediction_snapshots` が存在し、`app_version` 列がある場合、`v319_lap_snapshots_archive` を必要に応じて作成する。
8. `36445-36453`: 同じ保持外条件の行をsnapshot archiveへINSERTする。history側のような既存主キー除外条件はこのINSERTにはない。
9. `36454-36462`: 同じ保持外条件でsnapshot本体をDELETEし、件数をreportへ記録する。
10. `36463`: SQLite接続で変更をcommitする。
11. `36464-36467`: VACUUMを試行するが、失敗例外は握りつぶす。
12. `36468-36471`: reportをokにし、外側の例外はerror文字列に記録する。

対象テーブル:
- 本体: `v231_prediction_history`
- アーカイブ: `v319_prediction_history_archive`
- 本体: `v252_lap_prediction_snapshots`
- アーカイブ: `v319_lap_snapshots_archive`

削除条件は、両本体テーブルの `app_version` が `keep_versions` に含まれないこと（SQL上は `COALESCE(app_version,'') NOT IN (...)`）。UI既定保持候補にはVer322/323/324が含まれるが、UI利用者はmultiselectを変更できる。

### 1.2 `_v319_export_and_drop_archives()`

定義: `app.py:36305-36362`

1. `36308-36311`: reportを初期化し、出力先をDBと同じディレクトリの `<DB stem>_old_versions_backup.sqlite3` とする。
2. `36312-36320`: メインDBへ接続し、存在する `v319_prediction_history_archive` と `v319_lap_snapshots_archive` を列挙する。どちらもなければエラーreportを返す。
3. `36321-36334`: 出力先ファイルが既に存在する場合、`unlink()` で先に削除する。新規SQLiteを開き、各archive表の列情報からTEXT列のテーブルを作成した後、直ちにDROPするコードがある。ここで実データ転送は行わない。
4. `36335-36340`: メインDB接続から出力先DBを `archbak` としてATTACHし、各archive表を `CREATE TABLE archbak.<table> AS SELECT * FROM main.<table>` でコピーする。その後、メインDB側のarchive表をDROPし、件数をreportへ記録する。
5. `36341`: メインDB接続でcommitする。
6. `36342-36345`: DETACHを試行する。失敗例外は握りつぶす。
7. `36346-36349`: VACUUMを試行する。失敗は `vacuum_error` に記録するが、処理全体を失敗にはしない。
8. `36350-36359`: `ok=True`、出力先、ファイルサイズ等をreportへ設定する。
9. `36360-36362`: 外側の例外をerror文字列としてreportへ記録する。

重要なコード上の挙動:
- 既存バックアップファイルは検証・退避せずに先に削除される（36322-36323）。
- コピー後の件数照合、主キー照合、行ハッシュ照合、ファイルSHA-256、SQLite integrity_check、別DBへの復元試験はない。
- archive表は存在する方だけを出力対象とする。2表の両方が存在することを必須にしていない。
- VACUUM失敗は `vacuum_error` に残るが、`ok=True` となり得る（36346-36350）。
- DETACH失敗も無視される（36342-36345）。

## 2. UIからの呼び出し

`app.py:37611-37617` は保持バージョンmultiselectを表示する。既定保持候補にはVer322〜Ver326が含まれ、UIヘルプにもVer322〜Ver326を既定で残す旨がある。

`app.py:37618-37635` の「古いVer予測をバックアップへ退避」ボタンは、空選択を拒否した後、`_v319_archive_old_version_predictions()` を呼ぶ。

`app.py:37636-37649` の「アーカイブを別ファイルへ出して本体から削除」ボタンは、archive表が存在するかの簡易確認を関数内で行うだけで、直前の退避成功・件数一致・バックアップ検証を要求しない。

`app.py:37650-37654` は別ボタンで軽量GitHub保存を呼び出す。関数名は `push_db_to_github(..., _lightweight=True)`。この保存ボタンのcommit messageがGen676 DB commitの「DBを手動保存（軽量）」と一致するが、メッセージ一致だけでは操作順や操作者を証明しない。

## 3. 例外処理とロールバック範囲

### 3.1 archive関数

- DB接続は `with sqlite3.connect(...)` で管理され、明示commitは36463行。
- 36403-36471の外側try/exceptは例外をreportへ記録する。
- 36463より前に例外がwithブロック外へ伝播すれば、Python sqlite3 connection context managerの未commitトランザクションはrollback対象となる。
- 36463のcommit後に発生した例外を、既にcommitされたDELETEまで戻す処理はない。
- VACUUM例外は36466-36467で握りつぶされるため、VACUUM失敗でもreportはokになり得る。
- archiveへのINSERT件数と本体DELETE件数の不一致を検知して中断するガードはない。
- 明示的なSAVEPOINT、事前バックアップ、補償トランザクションはない。

### 3.2 export/drop関数

- 既存出力ファイルのunlinkは36322-36323で、コピー完了前に実行される。以前のバックアップを保持する仕組みはない。
- メインDB側archive表のDROPとATTACH先へのCREATE TABLEは36335-36340、commitは36341。
- commit前に例外が発生した場合、接続コンテキストのrollback対象となる未commit変更は戻る。ただし、既存出力ファイルのunlinkはSQLiteトランザクション外のファイル操作であり戻らない。
- 36341のcommit後は、本体archive表のDROPをrollbackする処理はない。
- VACUUMとDETACHの失敗はそれぞれ握りつぶし／記録だけで、処理成功判定を止めない。
- 出力ファイルの独立検証、fsync、atomic rename、復元テスト、ファイル保全ポリシーは実装されていない。

## 4. Gen675→Gen676差分との対応

監査済みDB差分:
- `v231_prediction_history`: 4,069 → 2,704（−1,365）
- `v252_lap_prediction_snapshots`: 18,822 → 10,626（−8,196）
- history削除1,365行はVer322=447、Ver323=447、Ver324=471。
- snapshot削除8,196行はVer322=2,682、Ver323=2,682、Ver324=2,832。
- 残存history 2,704行はGen675/676で全列ハッシュ一致。追加行は0。
- 削除対象の37 rootはGen675で37/37存在し、Gen676で0/37。
- Gen676のDB commit messageは「DBを手動保存（軽量）」。
- Gen675/676 DBにはarchive表が存在しない。
- 使い捨てSQLiteでarchive→DELETE→別SQLite export→DROP→VACUUMを再現し、対象2表のGen676行digestと一致した（Run #45146199262）。

このパターンは、Ver322〜324をkeep_versionsから外したarchive処理と整合する。UIの既定保持リストにはこれらのVerが含まれるため、当該経路で削除されたなら、実行時に保持選択を変更した必要がある。

## 5. 確定事実と未証明事項

### 確定事実
- 上記関数・UI・SQL・例外処理は対象コミットの実コードに存在する。
- Gen675→Gen676でVer322〜324の対象行が消え、その他の残存行は内容不変。
- 使い捨てSQLiteで同じ対象テーブル状態を再現できた。
- Gen675/676スナップショットにarchive表は存在しない。

### 未証明事項
- 2026-11-09に実際にどちらのUIボタンが押されたか。
- 実行時の `keep_versions` の値と操作者。
- archive関数の実行結果report、実際のINSERT/DELETE件数。
- `_old_versions_backup.sqlite3` が当時作成され、現在も保存されているか。
- 当時の実行で例外、VACUUM失敗、容量不足等が起きたか。
- Gen676のDB保存元で、削除処理が直前に行われたのか、既に削除済みDBを保存したのか。

したがって、旧Ver整理機能はGen675→Gen676差分の最有力説明だが、2026-11-09の実行イベントそのものは未証明とする。

## 一次設計レビューとの境界（2026-11-24）

この文書は既存コードの監査記録であり、安全化設計の承認を意味しない。一次レビュー判定は「要修正」。安全化設計書にR1〜R6の規範要件、テスト計画にT21〜T28を追補した。現行処理の事実認定や未証明事項は変更しない。

- SQLite WALを含む一貫snapshot、承認nonce、COMMIT_UNKNOWN、削除直前ロック、37 rootの本体保護、VACUUM分離は現行実装に存在すると確認したものではなく、今後の設計要件である。
- 2026-11-09の実操作イベント、操作者、keep_versions、専用archiveの存在は引き続き未証明。
- 本追補は設計レビュー対応のみ。app.py、本番DB、予測ロジック、APP_VERSION、Artifactは変更していない。

## 5. 安全化設計との境界（2026-11-24再レビュー追補）

本書の第1〜4節は対象ソースcommitに存在した旧実装の監査事実であり、今後の安全化仕様ではない。旧実装にBackup API snapshot、認証済み承認、nonce台帳、DB内operation receipt、外部fsync ledger、BEGIN IMMEDIATE後の最終digest照合、共通SQL traceは存在しない。これらが実装済みであると読み替えてはならない。

将来実装の規範仕様は次を唯一の参照先とする。
- SQLite snapshot、source_db_identity、WAL、承認、receipt、COMMIT_UNKNOWN、BEGIN IMMEDIATEの固定順序: issue31-safe-cleanup-design.md のR1〜R4。
- T01〜T28のfixture、共通証跡、期待結果: issue31-cleanup-test-plan.md。
- 本書は現行実装の事実記録を保持し、将来仕様を旧実装の挙動として記述しない。

今回の追補で現行コード、本番DB、予測ロジック、APP_VERSION、Artifactを変更していない。文書更新は安全性の実証・設計承認・実装承認を意味しない。
