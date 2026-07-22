# AutoRaceAI スマホ予測 v1

## 起動
```bash
pip install -r requirements.txt
streamlit run app.py
```

Streamlit Community CloudへZIP内の4ファイルをGitHubに置き、`app.py`を指定するとスマホから利用できます。

## 操作
1. autorace.jp の出走表を全文コピー
2. 入力欄へ貼り付け
3. 「予測する」を押す

## 現在の範囲
公式出走表内の試走、ST、ハンデ、平均競走T、ランク、審査P、走路別成績を使う軽量予測です。
選手の過去履歴DBを使うVer15.2完全ロジックは次段階で接続します。
