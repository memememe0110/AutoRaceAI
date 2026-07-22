# AutoRaceAI Streamlit Personal v2

GitHubでフォルダを作る必要はありません。

リポジトリの一番上に、次の5ファイルをそのままアップロードしてください。

- app.py
- engine.py
- mobile_helpers.py
- requirements.txt
- README.md

## Streamlitでの設定

- Repository: AutoRaceAI
- Branch: main
- Main file path: app.py

## 今回の修正

- `Workbook is not defined` を修正
- `.ipynb` を起動時に使用しない構成
- GitHub上でフォルダ追加不要
- SQLiteは実行時に自動作成
- DB管理画面からバックアップ可能

## 更新方法

GitHubで既存の同名ファイルを開き、編集するのではなく、
`Add file` → `Upload files` からこの5ファイルをまとめてアップロードしてください。
同名ファイルは新しい内容へ置き換わります。

アップロード後、Streamlitの Manage app から Reboot app を実行してください。
