"""
Aggregate completed regular-season games from seven Toronto calendar dates.

The window ends yesterday, so the 1 a.m. update includes the previous evening.
Games still in progress are picked up on the next update.

All players stay in data.json. The website selects the top 10 using each
visitor's scoring settings.

Missing statistics are null rather than incorrectly reported as zero.
Failed requests preserve the previous data.json.

Uses only Python's standard library.
"""

import json
import math
import time
import urllib.request

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


NHL_API = "https://api-web.nhle.com/v1"
DAYS_BACK = 7
OUT_PATH = Path(__file__).resolve().parent / "data.json"
LOCAL_ZONE = ZoneInfo("America/Toronto")


def get_json(url):
    for attempt in range(3):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "IceWatch/1.0",
                    "Accept": "application/json",
                },
            )

            with urllib.request.urlopen(request, timeout=30) as response:
                value = json.load(response)

            if not isinstance(value, dict):
                raise ValueError("Expected a JSON object: " + url)

            return value

        except Exception:
            if attempt == 2:
                raise

            time.sleep(attempt + 1)


def get_game_ids_for_range(days_back, end_date=None):
    end_date = (
        end_date
        or datetime.now(LOCAL_ZONE).date() - timedelta(days=1)
    )
    start_date = end_date - timedelta(days=days_back - 1)
    games = {}

    for offset in range(days_back):
        day = start_date + timedelta(days=offset)
        payload = get_json(f"{NHL_API}/schedule/{day.isoformat()}")

        if not isinstance(payload.get("gameWeek"), list):
            raise ValueError("Schedule response has no gameWeek array")

        # A schedule response can contain an entire week.
        # Explicitly enforce the requested seven-day range.
        for entry in payload["gameWeek"]:
            for game in entry.get("games", []):
                game_day = date.fromisoformat(
                    game.get("gameDate") or entry["date"]
                )

                if (
                    start_date <= game_day <= end_date
                    and str(game.get("gameType")) == "2"
                    and game.get("gameState") in ("OFF", "FINAL")
                ):
                    games[str(game["id"])] = game_day

    return sorted(games, key=lambda gid: (games[gid], gid))


def fetch_boxscore(game_id):
    return get_json(f"{NHL_API}/gamecenter/{game_id}/boxscore")


def number(value):
    if isinstance(value, bool) or value is None:
        return None

    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def add(row, key, value):
    value = number(value)

    # A missing value in any game makes the period total incomplete.
    if value is None or row.get(key) is None:
        row[key] = None
    else:
        row[key] += value


def player_name(player):
    name = player.get("name", "Unknown")

    if isinstance(name, dict):
        return name.get("default", "Unknown")

    return str(name)


def merge_skater(bucket, player, team_abbrev):
    row = bucket.setdefault(
        player["playerId"],
        {
            "name": player_name(player),
            "team": team_abbrev,
            "pos": player.get("position", "F"),
            **{
                key: 0
                for key in (
                    "g", "a", "pim", "ppp", "shg", "sha",
                    "gwg", "hat", "sog", "hit", "blk", "fow"
                )
            },
        },
    )

    row["team"] = team_abbrev

    fields = {
        "g": "goals",
        "a": "assists",
        "pim": "pim",
        "sog": "sog",
        "hit": "hits",
        "blk": "blockedShots",
        "shg": "shorthandedGoals",
        "sha": "shorthandedAssists",
        "gwg": "gameWinningGoals",
        "fow": "faceoffWins",
    }

    for key, source in fields.items():
        add(row, key, player.get(source))

    # PPP must include both power-play goals and assists.
    ppp = number(player.get("powerPlayPoints"))

    if ppp is None:
        goals = number(player.get("powerPlayGoals"))
        assists = number(player.get("powerPlayAssists"))

        if goals is not None and assists is not None:
            ppp = goals + assists

    add(row, "ppp", ppp)

    goals = number(player.get("goals"))
    add(row, "hat", int(goals >= 3) if goals is not None else None)


def played(player):
    toi = player.get("toi")

    if not isinstance(toi, str) or ":" not in toi:
        return None

    try:
        minutes, seconds = map(int, toi.split(":"))
        return minutes * 60 + seconds > 0
    except ValueError:
        return None


def merge_goalie(
    bucket,
    player,
    team_abbrev,
    box=None,
    side_goalies=None,
    opponent_score=None,
):
    if played(player) is False:
        return

    row = bucket.setdefault(
        player["playerId"],
        {
            "name": player_name(player),
            "team": team_abbrev,
            **{
                key: 0
                for key in ("gs", "w", "ga", "sv", "so", "otl")
            },
        },
    )

    row["team"] = team_abbrev

    starter = player.get("starter")
    add(row, "gs", int(starter) if isinstance(starter, bool) else None)

    decision = player.get("decision")
    add(row, "w", int(decision == "W"))
    add(row, "ga", player.get("goalsAgainst"))

    saves = number(player.get("saves"))
    shots = number(player.get("shotsAgainst"))
    goals = number(player.get("goalsAgainst"))

    if saves is None and shots is not None and goals is not None:
        saves = shots - goals

    add(row, "sv", saves)

    shutout = player.get("shutout")

    if not isinstance(shutout, (bool, int)):
        appearances = [played(g) for g in (side_goalies or [])]

        if (
            opponent_score is not None
            and appearances
            and None not in appearances
        ):
            shutout = (
                opponent_score == 0
                and appearances.count(True) == 1
                and played(player) is True
            )
        else:
            shutout = None

    add(row, "so", int(bool(shutout)) if shutout is not None else None)

    period = number(
        (box or {}).get("periodDescriptor", {}).get("number")
    )

    add(
        row,
        "otl",
        int(decision == "L" and period > 3)
        if period is not None
        else None,
    )


def main():
    end_date = datetime.now(LOCAL_ZONE).date() - timedelta(days=1)
    start_date = end_date - timedelta(days=DAYS_BACK - 1)

    game_ids = get_game_ids_for_range(DAYS_BACK, end_date)

    print(
        f"{start_date} through {end_date}: "
        f"{len(game_ids)} completed regular-season games"
    )

    skaters = {}
    goalies = {}

    for game_id in game_ids:
        box = fetch_boxscore(game_id)

        # Verify regular season again before aggregating the boxscore.
        if (
            str(box.get("gameType")) != "2"
            or box.get("gameState") not in ("OFF", "FINAL")
        ):
            raise ValueError(
                f"Unexpected game type/state for {game_id}; "
                "previous data retained"
            )

        if not (
            start_date
            <= date.fromisoformat(box["gameDate"])
            <= end_date
        ):
            raise ValueError(f"Out-of-range boxscore {game_id}")

        stats = box.get("playerByGameStats")

        if not isinstance(stats, dict):
            raise ValueError(f"Missing player statistics for {game_id}")

        for side, opponent in (
            ("awayTeam", "homeTeam"),
            ("homeTeam", "awayTeam"),
        ):
            group = stats.get(side)

            if (
                not isinstance(group, dict)
                or not all(
                    isinstance(group.get(key), list)
                    for key in ("forwards", "defense", "goalies")
                )
            ):
                raise ValueError(
                    f"Incomplete player groups for {game_id}/{side}"
                )

            team = box[side]["abbrev"]

            for player in group["forwards"] + group["defense"]:
                merge_skater(skaters, player, team)

            for player in group["goalies"]:
                merge_goalie(
                    goalies,
                    player,
                    team,
                    box,
                    group["goalies"],
                    number(box[opponent].get("score")),
                )

    waivers = []

    if OUT_PATH.exists():
        try:
            waivers = json.loads(OUT_PATH.read_text()).get("waivers", [])
        except (ValueError, AttributeError):
            pass

    data = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "game_type": 2,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "timezone": "America/Toronto",
        "games_count": len(game_ids),
        "skaters": list(skaters.values()),
        "goalies": list(goalies.values()),
        "waivers": waivers,
    }

    temporary_path = OUT_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(data, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(OUT_PATH)

    print(
        f"Wrote {len(skaters)} skaters and "
        f"{len(goalies)} goalies to {OUT_PATH}"
    )


if __name__ == "__main__":
    main()
