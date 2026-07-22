# AutoRaceAI スマホ本予測 v2.7

- Ver13系SQLite DB（players / race_history）の読込に対応
- アップロード後に登録選手・履歴件数を正しく表示
- 前走〜10走前形式をVer13系DBと詳細ミラー表へ登録
- 登録履歴を予測本体がそのまま使用
- 展開予想（先行縦長・前残り・混戦・追い込み）を%表示
- 三連単確率を%表示
- GitHubへのDB保存・復元

Streamlit Secretsには GITHUB_TOKEN / GITHUB_REPO / GITHUB_BRANCH / GITHUB_DB_PATH を設定してください。
