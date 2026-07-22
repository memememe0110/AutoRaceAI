# AutoRaceAI スマホ本予測 v2

Ver15.2の設定値、player_metrics、calculate_excel_model、モンテカルロを移植したStreamlit版です。

## 起動
```bash
pip install -r requirements.txt
streamlit run app.py
```

## 重要
元版と同じ予測結果にするには、元版で使用した `autorace_players.sqlite3` を画面左から読み込んでください。履歴DBが異なると結果も異なります。
