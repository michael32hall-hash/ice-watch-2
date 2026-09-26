"""Ice Watchers raw-performance data generator.

Produces:
- Existing rolling 7-day skater/goalie totals (backward compatible)
- Existing rolling 30-day totals under "monthly" (backward compatible)
- Season-to-date totals under "season"
- Per-player game logs under "game_logs"

NHL gameType 2 denotes the regular season.
Only completed games (OFF / FINAL) are included.
Missing stats are null, never fabricated as zero.
A failed request aborts the update and preserves the previous data.json.

Uses only Python 3.12's standard library.
"""

import json
import math
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NHL_API = "https://api-web.nhle.com/v1"
DAYS_BACK = 7
MONTH_DAYS = 30
OUT_PATH = Path(__file__).resolve().parent / "data.json"
LOCAL_ZONE = ZoneInfo("America/Toronto")


def get_json(url):
    for attempt in range(3):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "IceWatch/2.0",
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


def get_game_ids_for_range(start_date, end_date):
    """Return completed regular-season game IDs in an inclusive date range."""
    games = {}

    day = start_date
    while day <= end_date:
        payload = get_json(f"{NHL_API}/schedule/{day.isoformat()}")

        if not isinstance(payload.get("gameWeek"), list):
            raise ValueError("Schedule response has no gameWeek array")

        for entry in payload["gameWeek"]:
            for game in entry.get("games", []):
                game_day = date.fromisoformat(game.get("gameDate") or entry["date"])

                if (
                    start_date <= game_day <= end_date
                    and str(game.get("gameType")) == "2"
                    and game.get("gameState") in ("OFF", "FINAL")
                ):
                    games[str(game["id"])] = game_day

        day += timedelta(days=1)

    return sorted(games, key=lambda gid: (games[gid], gid))


def fetch_boxscore(game_id):
    return get_json(f"{NHL_API}/gamecenter/{game_id}/boxscore")


def number(value):
    if isinstance(value, bool) or value is None:
        return None

    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def clean_number(value):
    """Convert whole floats to ints while preserving decimals and None."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def add(row, key, value):
    n = number(value)
    row[key] = None if n is None or row.get(key) is None else row[key] + n


def player_name(player):
    name = player.get("name", "Unknown")
    return name.get("default", "Unknown") if isinstance(name, dict) else str(name)


def toi_seconds(value):
    if not isinstance(value, str) or ":" not in value:
        return None

    try:
        minutes, seconds = map(int, value.split(":"))
        return minutes * 60 + seconds
    except ValueError:
        return None


def format_toi(seconds):
    if seconds is None:
        return None

    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def played(player):
    seconds = toi_seconds(player.get("toi"))
    return None if seconds is None else seconds > 0


def new_skater_row(player, team_abbrev):
    return dict(
        name=player_name(player),
        team=team_abbrev,
        pos=player.get("position", "F"),
        player_id=player["playerId"],
        **{
            key: 0
            for key in (
                "g",
                "a",
                "pim",
                "ppp",
                "shg",
                "sha",
                "gwg",
                "hat",
                "sog",
                "hit",
                "blk",
                "fow",
                "plusMinus",
                "gp",
                "seconds",
            )
        },
    )


def merge_skater(bucket, player, team_abbrev):
    row = bucket.setdefault(
        player["playerId"], new_skater_row(player, team_abbrev)
    )

    row["team"] = team_abbrev
    row["player_id"] = player["playerId"]
    row["pos"] = player.get("position", row.get("pos", "F"))

    add(row, "gp", 1)

    for key, source in {
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
        "plusMinus": "plusMinus",
    }.items():
        add(row, key, player.get(source))

    ppp = number(player.get("powerPlayPoints"))
    if ppp is None:
        goals = number(player.get("powerPlayGoals"))
        assists = number(player.get("powerPlayAssists"))
        ppp = (
            goals + assists
            if goals is not None and assists is not None
            else None
        )

    add(row, "ppp", ppp)

    goals = number(player.get("goals"))
    add(row, "hat", int(goals >= 3) if goals is not None else None)
    add(row, "seconds", toi_seconds(player.get("toi")))


def new_goalie_row(player, team_abbrev):
    return dict(
        name=player_name(player),
        team=team_abbrev,
        player_id=player["playerId"],
        **{
            key: 0
            for key in (
                "gs",
                "w",
                "ga",
                "sv",
                "so",
                "otl",
                "gp",
                "sa",
                "seconds",
            )
        },
    )


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
        player["playerId"], new_goalie_row(player, team_abbrev)
    )

    row["team"] = team_abbrev
    row["player_id"] = player["playerId"]

    add(row, "gp", 1)
    add(
        row,
        "gs",
        int(player["starter"])
        if isinstance(player.get("starter"), bool)
        else None,
    )

    decision = player.get("decision")
    add(row, "w", int(decision == "W"))
    add(row, "ga", player.get("goalsAgainst"))

    saves = number(player.get("saves"))
    shots = number(player.get("shotsAgainst"))
    goals = number(player.get("goalsAgainst"))

    if saves is None and shots is not None and goals is not None:
        saves = shots - goals

    add(row, "sv", saves)
    add(
        row,
        "sa",
        shots
        if shots is not None
        else (
            saves + goals
            if saves is not None and goals is not None
            else None
        ),
    )

    duration = toi_seconds(player.get("toi"))
    add(row, "seconds", duration)

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

    add(
        row,
        "so",
        int(bool(shutout)) if shutout is not None else None,
    )

    period = number((box or {}).get("periodDescriptor", {}).get("number"))
    add(
        row,
        "otl",
        int(decision == "L" and period > 3)
        if period is not None
        else None,
    )


def finalize_skaters(rows):
    for player in rows.values():
        gp = number(player.get("gp"))
        seconds = number(player.get("seconds"))

        player["toi"] = format_toi(seconds)
        player["toiPerGame"] = (
            seconds / gp if seconds is not None and gp and gp > 0 else None
        )
        player["avgToi"] = format_toi(player["toiPerGame"])

        for key in (
            "g",
            "a",
            "pim",
            "ppp",
            "shg",
            "sha",
            "gwg",
            "hat",
            "sog",
            "hit",
            "blk",
            "fow",
            "plusMinus",
            "gp",
            "seconds",
        ):
            player[key] = clean_number(player.get(key))


def finalize_goalies(rows):
    for player in rows.values():
        saves = number(player.get("sv"))
        shots = number(player.get("sa"))
        goals = number(player.get("ga"))
        seconds = number(player.get("seconds"))
        gp = number(player.get("gp"))

        player["svPct"] = (
            saves / shots
            if saves is not None and shots is not None and shots > 0
            else None
        )
        player["gaa"] = (
            goals * 3600 / seconds
            if goals is not None and seconds is not None and seconds > 0
            else None
        )
        player["toi"] = format_toi(seconds)
        player["toiPerGame"] = (
            seconds / gp if seconds is not None and gp and gp > 0 else None
        )
        player["avgToi"] = format_toi(player["toiPerGame"])

        for key in (
            "gs",
            "w",
            "ga",
            "sv",
            "so",
            "otl",
            "gp",
            "sa",
            "seconds",
        ):
            player[key] = clean_number(player.get(key))


def aggregate(boxes, start_date, end_date):
    skaters = {}
    goalies = {}
    count = 0

    for box in boxes:
        game_date = date.fromisoformat(box["gameDate"])

        if not start_date <= game_date <= end_date:
            continue

        count += 1

        stats = box.get("playerByGameStats")
        if not isinstance(stats, dict):
            raise ValueError(
                f"Missing player statistics for {box.get('id')}"
            )

        for side, opponent in (
            ("awayTeam", "homeTeam"),
            ("homeTeam", "awayTeam"),
        ):
            group = stats.get(side)

            if (
                not isinstance(group, dict)
                or not all(
                    isinstance(group.get(k), list)
                    for k in ("forwards", "defense", "goalies")
                )
            ):
                raise ValueError(
                    f"Incomplete statistics for {box.get('id')}/{side}"
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

    finalize_skaters(skaters)
    finalize_goalies(goalies)

    return dict(
        game_type=2,
        period_start=start_date.isoformat(),
        period_end=end_date.isoformat(),
        games_count=count,
        skaters=list(skaters.values()),
        goalies=list(goalies.values()),
    )


def skater_game_log(player, team, opponent, game_id, game_date, home_away):
    ppp = number(player.get("powerPlayPoints"))

    if ppp is None:
        pp_goals = number(player.get("powerPlayGoals"))
        pp_assists = number(player.get("powerPlayAssists"))
        ppp = (
            pp_goals + pp_assists
            if pp_goals is not None and pp_assists is not None
            else None
        )

    goals = number(player.get("goals"))

    return {
        "game_id": int(game_id),
        "date": game_date,
        "team": team,
        "opponent": opponent,
        "home_away": home_away,
        "pos": player.get("position", "F"),
        "g": clean_number(number(player.get("goals"))),
        "a": clean_number(number(player.get("assists"))),
        "pim": clean_number(number(player.get("pim"))),
        "ppp": clean_number(ppp),
        "shg": clean_number(number(player.get("shorthandedGoals"))),
        "sha": clean_number(number(player.get("shorthandedAssists"))),
        "gwg": clean_number(number(player.get("gameWinningGoals"))),
        "hat": int(goals >= 3) if goals is not None else None,
        "sog": clean_number(number(player.get("sog"))),
        "hit": clean_number(number(player.get("hits"))),
        "blk": clean_number(number(player.get("blockedShots"))),
        "fow": clean_number(number(player.get("faceoffWins"))),
        "plusMinus": clean_number(number(player.get("plusMinus"))),
        "toi": player.get("toi"),
        "toi_seconds": toi_seconds(player.get("toi")),
    }


def goalie_game_log(
    player,
    team,
    opponent,
    game_id,
    game_date,
    home_away,
    box,
    side_goalies,
    opponent_score,
):
    if played(player) is False:
        return None

    saves = number(player.get("saves"))
    shots = number(player.get("shotsAgainst"))
    goals = number(player.get("goalsAgainst"))

    if saves is None and shots is not None and goals is not None:
        saves = shots - goals

    if shots is None and saves is not None and goals is not None:
        shots = saves + goals

    duration = toi_seconds(player.get("toi"))
    decision = player.get("decision")

    shutout = player.get("shutout")

    if not isinstance(shutout, (bool, int)):
        appearances = [played(g) for g in side_goalies]

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

    period = number((box or {}).get("periodDescriptor", {}).get("number"))

    return {
        "game_id": int(game_id),
        "date": game_date,
        "team": team,
        "opponent": opponent,
        "home_away": home_away,
        "starter": player.get("starter")
        if isinstance(player.get("starter"), bool)
        else None,
        "decision": decision,
        "w": int(decision == "W"),
        "otl": (
            int(decision == "L" and period > 3)
            if period is not None
            else None
        ),
        "ga": clean_number(goals),
        "sv": clean_number(saves),
        "sa": clean_number(shots),
        "svPct": (
            saves / shots
            if saves is not None and shots is not None and shots > 0
            else None
        ),
        "so": int(bool(shutout)) if shutout is not None else None,
        "toi": player.get("toi"),
        "toi_seconds": duration,
        "gaa": (
            goals * 3600 / duration
            if goals is not None and duration is not None and duration > 0
            else None
        ),
    }


def build_game_logs(boxes):
    """Create chronological per-player logs for future trend calculations."""
    skaters = defaultdict(
        lambda: {"name": None, "team": None, "pos": None, "games": []}
    )
    goalies = defaultdict(
        lambda: {"name": None, "team": None, "pos": "G", "games": []}
    )

    for box in sorted(boxes, key=lambda b: (b["gameDate"], int(b["id"]))):
        stats = box["playerByGameStats"]
        game_id = box["id"]
        game_date = box["gameDate"]

        for side, opponent_side, home_away in (
            ("awayTeam", "homeTeam", "away"),
            ("homeTeam", "awayTeam", "home"),
        ):
            group = stats[side]
            team = box[side]["abbrev"]
            opponent = box[opponent_side]["abbrev"]
            opponent_score = number(box[opponent_side].get("score"))

            for player in group["forwards"] + group["defense"]:
                pid = str(player["playerId"])
                entry = skaters[pid]
                entry["name"] = player_name(player)
                entry["team"] = team
                entry["pos"] = player.get("position", "F")
                entry["games"].append(
                    skater_game_log(
                        player,
                        team,
                        opponent,
                        game_id,
                        game_date,
                        home_away,
                    )
                )

            for player in group["goalies"]:
                log = goalie_game_log(
                    player,
                    team,
                    opponent,
                    game_id,
                    game_date,
                    home_away,
                    box,
                    group["goalies"],
                    opponent_score,
                )

                if log is None:
                    continue

                pid = str(player["playerId"])
                entry = goalies[pid]
                entry["name"] = player_name(player)
                entry["team"] = team
                entry["games"].append(log)

    return {
        "skaters": dict(skaters),
        "goalies": dict(goalies),
    }


def season_start_for(end_date):
    """NHL regular seasons begin in the fall; July 1 is a safe season boundary."""
    year = end_date.year if end_date.month >= 7 else end_date.year - 1
    return date(year, 7, 1)


def main():
    end_date = datetime.now(LOCAL_ZONE).date() - timedelta(days=1)
    week_start = end_date - timedelta(days=DAYS_BACK - 1)
    month_start = end_date - timedelta(days=MONTH_DAYS - 1)
    season_start = season_start_for(end_date)

    ids = get_game_ids_for_range(season_start, end_date)

    print(
        f"{season_start} through {end_date}: "
        f"{len(ids)} completed regular-season games"
    )

    with ThreadPoolExecutor(max_workers=6) as pool:
        boxes = list(pool.map(fetch_boxscore, ids))

    for box in boxes:
        if (
            str(box.get("gameType")) != "2"
            or box.get("gameState") not in ("OFF", "FINAL")
        ):
            raise ValueError(
                "Unexpected game type/state; previous data retained"
            )

        if not season_start <= date.fromisoformat(box["gameDate"]) <= end_date:
            raise ValueError(
                "Out-of-range boxscore; previous data retained"
            )

    # Existing website data.
    data = aggregate(boxes, week_start, end_date)
    data["monthly"] = aggregate(boxes, month_start, end_date)

    # Raw Performance v1.
    data["season"] = aggregate(boxes, season_start, end_date)
    data["game_logs"] = build_game_logs(boxes)

    data["raw_performance"] = {
        "version": 1,
        "season_start": season_start.isoformat(),
        "through": end_date.isoformat(),
        "windows": {
            "week_days": DAYS_BACK,
            "month_days": MONTH_DAYS,
        },
        "description": (
            "Season totals and chronological game logs used by "
            "Ice Watchers trend intelligence."
        ),
    }

    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["timezone"] = "America/Toronto"

    # Preserve any manually maintained waiver data.
    data["waivers"] = []

    if OUT_PATH.exists():
        try:
            old = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            data["waivers"] = old.get("waivers", [])
        except (ValueError, AttributeError):
            pass

    temp = OUT_PATH.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(data, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temp.replace(OUT_PATH)

    print(
        "Wrote Ice Watchers Raw Performance v1: "
        f"{len(data['skaters'])} weekly skaters, "
        f"{len(data['monthly']['skaters'])} monthly skaters, "
        f"{len(data['season']['skaters'])} season skaters, "
        f"{len(data['game_logs']['skaters'])} skater logs, "
        f"{len(data['game_logs']['goalies'])} goalie logs"
    )


if __name__ == "__main__":
    main()
