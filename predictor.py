def predict_race(race):
    players = []
    for p in race.get("players", []):
        score = (9 - p.get("車番", 8)) * 0.02
        if p.get("ハンデ") is not None:
            score -= p["ハンデ"] * 0.015
        if p.get("試走T") is not None:
            score += max(0, 4.2 - p["試走T"]) * 3
        row = dict(p)
        row["_score"] = score
        players.append(row)

    players.sort(key=lambda x: x["_score"], reverse=True)
    weights = [max(0.01, x["_score"] + 1) for x in players]
    total = sum(weights) or 1

    return [{
        "予測順位": i,
        "車番": p["車番"],
        "選手名": p["選手名"],
        "仮1着率": round(w / total * 100, 1),
    } for i, (p, w) in enumerate(zip(players, weights), 1)]
