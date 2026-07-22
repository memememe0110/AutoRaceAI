# AutoRaceAI スマホ本予測 v2.6

## 追加機能
- 公式プロフィールの「前走〜10走前」形式を解析
- 選手履歴をSQLiteへ重複なしで登録
- 登録前プレビュー
- 選手別の登録情報確認
- 確率を `%` 表示
- GitHubへDBを保存し、アプリ起動時に復元
- DBの手動ダウンロード・インポート

## Streamlit Secrets
Streamlit Community Cloudのアプリ設定から次を登録してください。

```toml
GITHUB_TOKEN = "github_pat_xxxxxxxxx"
GITHUB_REPO = "ユーザー名/リポジトリ名"
GITHUB_BRANCH = "main"
GITHUB_DB_PATH = "autorace_players.sqlite3"
```

`GITHUB_TOKEN`には対象リポジトリのContentsを読み書きできる権限が必要です。
トークンを `app.py` やGitHub上のファイルへ直接書かないでください。

## DB保持の仕組み
1. アプリ起動時にGitHub上のDBを取得
2. 選手情報登録後にSQLiteを更新
3. 更新したDBをGitHubへコミット
4. 再起動・再デプロイ後もGitHubから復元

GitHub保存に失敗した場合でも、サイドバーからDBを端末へ保存できます。
