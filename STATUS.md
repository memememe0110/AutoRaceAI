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
| 進行中 | 予測 | [#31](https://github.com/memememe0110/AutoRaceAI/issues/31) | 予測データの再現性・保存整合性 | Run #8で実DB generation 705の重複監査完了。3228行/540重複race/2331重複行。実DB12列・NOT NULL制約確認、DB SHA不変、integrity_check=ok。Artifact ID 14642433960 | 48組188行を時系列再分析。Ver326/20,000の同一settings_hash・seed・trials・input SHAの完全一致fingerprintが37組で複数回保存（35組×3回、2組×2回、計109行）。10/15〜10/16の複数波で、隣接間隔最短11,036秒/中央値31,500秒。60秒未満・1時間未満0。Ver327は10/22〜23、Ver328は10/26に版別波。大規模9組559行も96完全fingerprint群中80群で反復（543行）。Ver326/327/328の版別バッチ波と重複が混在。短周期ポーリングは否定的だが、Ver326同一条件の複数回実行経路は未特定。PR #44（11/17作成・11/18マージ）で履歴再利用fingerprintを厳格化し、PR #47（11/18マージ）でinput SHA欠損時の抜け穴を修正（旧実装はcomplete判定にinput SHAなし、raw_text SHA照合もif input_sha256ガード内。現行は欠損SHAをfail-closed拒否し常時SHA照合）。ただし反復の中心は10/15〜16で、PR #44/#47は11月の変更のため、10月事象の原因とは断定不可。10月当時の呼出元・履歴再利用経路を遡及確認する。DB読み取り専用、保存関数修正なし。Run #8: 9 tests PASS / audit SUCCESS。#28隔離、#29変更なし |

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
