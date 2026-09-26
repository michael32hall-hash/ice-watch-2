"""
Pulls the last 7 days of completed NHL games from the NHL's public (unofficial)
API and aggregates per-player stats into the shape data.json expects.

IMPORTANT: This uses api-web.nhle.com, which is not officially documented and
can change without notice. The field names below are best-effort based on the
commonly-observed shape of the boxscore response. If a KeyError shows up in
the Actions log, print(json.dumps(box, indent=2)) for one game and check the
actual field name, then fix it here — happy to help debug once you have a
real response to look at.

Not available (defaults to 0, safe to leave unless you want to dig further):
  - SHG / SHA (shorthanded goals/assists): not broken out per-player in the
    basic boxscore endpoint.
  - GWG (game-winning goal): same limitation.
  - FOW (faceoff wins as a count): boxscore exposes a *percentage*
    (faceoffWinningPctg), not a raw count, so this is left at 0 for now.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

NHL_API = "https://api-web.nhle.com/v1"
DAYS_BACK = 7
OUT_PATH = Path(__file__).resolve().parent.parent / "data.json"


def get_game_ids_for_range(days_back: int) -> list[str]:
    game_ids = []
    for i in range(days_back):
        d = (date.today() - timedelta(days=i)).isoformat()
        try:
            resp = requests.get(f"{NHL_API}/schedule/{d}", timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  schedule fetch failed for {d}: {e}", file=sys.stderr)
            continue
        payload = resp.json()
        for week in payload.get("gameWeek", []):
            for game in week.get("games", []):
                if game.get("gameState") in ("OFF", "FINAL"):
                    game_ids.append(game["id"])
    return sorted(set(game_ids))


def fetch_boxscore(game_id: str) -> dict:
    resp = requests.get(f"{NHL_API}/gamecenter/{game_id}/boxscore", timeout=15)
    resp.raise_for_status()
    return resp.json()


def merge_skater(bucket: dict, p: dict, team_abbrev: str):
    key = p["playerId"]
    row = bucket.setdefault(key, {
        "name": p.get("name", {}).get("default", "Unknown"),
        "team": team_abbrev,
        "pos": "D" if p.get("position") == "D" else "F",
        "g": 0, "a": 0, "pim": 0, "ppp": 0, "shg": 0, "sha": 0,
        "gwg": 0, "hat": 0, "sog": 0, "hit": 0, "blk": 0, "fow": 0,
    })
    row["g"] += p.get("goals", 0)
    row["a"] += p.get("assists", 0)
    row["pim"] += p.get("pim", 0)
    row["ppp"] += p.get("powerPlayGoals", 0)  # best-effort; PP assists not separately exposed here
    row["sog"] += p.get("sog", 0)
    row["hit"] += p.get("hits", 0)
    row["blk"] += p.get("blockedShots", 0)
    if p.get("goals", 0) >= 3:
        row["hat"] = 1


def merge_goalie(bucket: dict, p: dict, team_abbrev: str):
    key = p["playerId"]
    row = bucket.setdefault(key, {
        "name": p.get("name", {}).get("default", "Unknown"),
        "team": team_abbrev,
        "gs": 0, "w": 0, "ga": 0, "sv": 0, "so": 0, "otl": 0,
    })
    if p.get("toi") and p["toi"] != "00:00":
        row["gs"] += 1
    decision = p.get("decision")  # expected "W" / "L" / None
    if decision == "W":
        row["w"] += 1
    row["ga"] += p.get("goalsAgainst", 0)
    row["sv"] += p.get("saves", 0)
    if p.get("shutout") or p.get("goalsAgainst", 0) == 0 and decision == "W":
        row["so"] += 1
    # NHL boxscore doesn't reliably flag OT losses per-goalie; leaving at 0
    # unless you want to cross-reference the game's periodDescriptor.


def main():
    print("Finding completed games in the last week...")
    game_ids = get_game_ids_for_range(DAYS_BACK)
    print(f"Found {len(game_ids)} completed games.")

    skater_bucket, goalie_bucket = {}, {}

    for gid in game_ids:
        try:
            box = fetch_boxscore(gid)
        except requests.RequestException as e:
            print(f"  boxscore fetch failed for {gid}: {e}", file=sys.stderr)
            continue

        stats = box.get("playerByGameStats", {})
        for side in ("awayTeam", "homeTeam"):
            side_data = stats.get(side, {})
            team_abbrev = box.get(side, {}).get("abbrev", "???")
            for p in side_data.get("forwards", []) + side_data.get("defense", []):
                merge_skater(skater_bucket, p, team_abbrev)
            for p in side_data.get("goalies", []):
                merge_goalie(goalie_bucket, p, team_abbrev)

    data = {}
    if OUT_PATH.exists():
        data = json.loads(OUT_PATH.read_text())

    from datetime import datetime, timezone
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["skaters"] = list(skater_bucket.values())
    data["goalies"] = list(goalie_bucket.values())
    data.setdefault("waivers", [])

    OUT_PATH.write_text(json.dumps(data, indent=2))
    print(f"Wrote {len(data['skaters'])} skaters and {len(data['goalies'])} goalies to {OUT_PATH}")


if __name__ == "__main__":
    main()
