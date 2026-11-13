# qver_lock8_w07 レビュー修正（ブランチ `qver-lock8-review`）

main は変更していません。採用するかどうかは、差分を確認したうえで別途判断します。

## コミット構成（main cd3e231 から）

| commit | 内容 |
|---|---|
| 392e9aa | 受け取った app.py / engine.py を3者マージ（元 40e98b7f、受取版、main ce89f3d）。**修正なし**。65c62b9（天候）、5c945ff（日付の高速化）、Ver331 診断は残してある |
| 05442f8 | 指摘1＋2：保存関数のトランザクション（TX）境界をそろえ、Ver266 を Worker の TX に参加させる |
| b62ee79 | 指摘3：heartbeat を全終了経路で停止・join |
| （このコミット） | 試験 `tests/qver_lock8_review_test.py` と本書 |

`git diff 392e9aa..HEAD` で、受け取った版からの修正分だけを確認できます。

## TX 境界（保存関数共通の規約）

| | 自前接続（con=None） | 外部接続（con 指定） |
|---|---|---|
| 表の作成 | する | **しない**（呼び出し側が BEGIN 前に `_v326_preensure_save_tables()` で用意する） |
| 成功時 | commit | commit しない |
| 例外時 | rollback して再送出 | rollback しない（例外は再送出。呼び出し側が ROLLBACK） |
| commit=False | rollback（途中結果を残さない。互換用の引数） | 影響なし |

- 対象：engine の `v34_save_prediction_snapshot`、`v40_save_prediction_features`、`v67_save_ticket_snapshot`、app の `_v231_save_prediction_history`、`_v222_save_prediction_restore`、`_v266_save_pred_time_snapshot`。
- `_v231` は自前接続のときだけ、確定後に Ver266 を別の TX で best-effort 保存する（従来どおり）。外部接続のときは呼び出し側が同じ TX の中で `_v266(con=...)` を呼ぶ。
- Worker の所有権 TX では、`_v326_preensure_save_tables` → `BEGIN IMMEDIATE` → 所有権の確認 → v34 → v231（SAVEPOINT、slim 再試行）→ v222 → v266（SAVEPOINT、同じ接続）→ v67 / v40 → queue done（rowcount=1）→ COMMIT の順に進む。接続は `closing()` で閉じる。
- 共有 TX の中で、別の接続から書き込む処理は残っていない。v231 の `v305_capture_model_state_snapshot` は読み取りだけ（WAL なのでロックで待たない）。

## heartbeat

- スレッドは、1タスク分の処理を囲む try の中で開始する。
- 冪等な `_hb_shutdown()` を次の3か所で呼ぶ：取り込み呼び出しの finally、状態更新の直前、1タスクを囲む try の finally。
- これにより、通常・履歴のみ・版設定エラー（continue）・中止（return）・予測エラー・例外・所有権喪失のどの経路でもスレッドが止まる。

## 指摘4：settings_hash の `|qcfg:` 付与
- 既存のコードで `settings_hash` を**一致判定に使っている箇所はない**。表示（UI の「設定ID」は view の値で、DB 列ではない）と一覧での取得だけ。
- `|qcfg:` を使うのは、新しく加わった `_v326_find_matching_prediction_history` だけ。`|qcfg:` の無い既存の行は、ここで照合の対象外になる（意図どおりの動作）。
- 収集側の tools/ と評価スクリプトは `settings_hash` を使っていない。
- 注意：DB 列にだけ `|qcfg:` が付き、payload 内の view["settings_hash"] には付かない。今のところ実害はない。

## 試験結果（サンドボックス、11/13）

`python tests/qver_lock8_review_test.py --app app.py --engine engine.py [--db 本番DBコピー --card-db 収集ストア]`

| 試験 | 受取版（392e9aa） | 修正版（HEAD） |
|---|---|---|
| T1 自前接続で途中失敗（トリガで3行目を失敗させる） | ✗ v34・v40・v67 で既存4行が消え、途中の2行（v67 は1行）が残る | ✓ 6関数とも既存行がそのまま残る |
| T2 外部接続 → ROLLBACK で 0 行、COMMIT で全行、ロック待ちなし | ✗（v266 に con 引数がない） | ✓ 6関数とも、ROLLBACK で 0 行・COMMIT で全行・0.004 秒 |
| T3 heartbeat の終了経路7通り | ✗ 履歴のみと版設定エラーでスレッド `qhb-7` が残り、終了後も heartbeat が続く | ✓ 7経路ともスレッド 0、終了後の heartbeat 0 |
| T4 実予測 1000 試行（本番 DB の generation 688 のコピー、20261112 伊勢崎 5R） | ✗ v266 は 0 行のまま「成功」扱い | ✓ 別 token は何も変えずに ownership_lost を返す。正しい token は v34/v40/v266 が各7行、v231/v222 が各1行、v67 が307行、queue が done |

- T1・T2・T3 は一時 DB での試験。T3 は Worker 関数を AST で取り出し、周辺を差し替えて実行した。
- T4 は本番 DB の**コピー**を一時ディレクトリで使っていて、本番 DB には書き込んでいない。

## 未確認のこと
- Streamlit 上での実際の動作（UI 経路と Worker の 1R 処理、長時間レース中の updated_at 更新）。
- Phase 5 相当の統合試験（複数 Worker の競合、stale からの回収）。
- 受取版にあった「H01–H18 の全試験」は、このブランチには入れていない。
