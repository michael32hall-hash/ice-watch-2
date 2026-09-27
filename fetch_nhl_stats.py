"""Ice Watchers NHL data generator - Raw Performance + Near Live.

Permanent totals/history use COMPLETED regular-season games only.
In-progress regular-season games are written separately under data["live"].

Existing site-compatible keys:
  skaters, goalies, monthly, season, game_logs, waivers

New near-live key:
  live = {
    "games_count": ...,
    "games": [...],
    "skaters": [...],
    "goalies": [...],
    "updated_at": ...
  }

Python 3.12 standard library only.
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

FINAL_STATES = {"OFF", "FINAL"}
LIVE_STATES = {"LIVE", "CRIT"}


def get_json(url):
    for attempt in range(3):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "IceWatch/3.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                value = json.load(response)
            if not isinstance(value, dict):
                raise ValueError("Expected JSON object: " + url)
            return value
        except Exception:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def clean_number(value):
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


def season_start_for(end_date):
    year = end_date.year if end_date.month >= 7 else end_date.year - 1
    return date(year, 7, 1)


def schedule_games_for_date(day):
    payload = get_json(f"{NHL_API}/schedule/{day.isoformat()}")
    if not isinstance(payload.get("gameWeek"), list):
        raise ValueError("Schedule response has no gameWeek array")

    games = []
    for entry in payload["gameWeek"]:
        for game in entry.get("games", []):
            game_day = date.fromisoformat(game.get("gameDate") or entry["date"])
            if game_day == day and str(game.get("gameType")) == "2":
                games.append(game)
    return games


def completed_game_ids_for_range(start_date, end_date):
    games = {}
    day = start_date
    while day <= end_date:
        for game in schedule_games_for_date(day):
            if game.get("gameState") in FINAL_STATES:
                games[str(game["id"])] = day
        day += timedelta(days=1)
    return sorted(games, key=lambda gid: (games[gid], gid))


def live_games_for_dates(days):
    games = {}
    for day in days:
        for game in schedule_games_for_date(day):
            if game.get("gameState") in LIVE_STATES:
                games[str(game["id"])] = game
    return list(games.values())


def fetch_boxscore(game_id):
    return get_json(f"{NHL_API}/gamecenter/{game_id}/boxscore")


def new_skater_row(player, team):
    return dict(
        name=player_name(player),
        team=team,
        pos=player.get("position", "F"),
        player_id=player["playerId"],
        **{k: 0 for k in (
            "g", "a", "pim", "ppp", "shg", "sha", "gwg", "hat",
            "sog", "hit", "blk", "fow", "plusMinus", "gp", "seconds"
        )},
    )


def merge_skater(bucket, player, team):
    row = bucket.setdefault(player["playerId"], new_skater_row(player, team))
    row["team"] = team
    row["player_id"] = player["playerId"]
    row["pos"] = player.get("position", row.get("pos", "F"))
    add(row, "gp", 1)

    for key, source in {
        "g": "goals", "a": "assists", "pim": "pim", "sog": "sog",
        "hit": "hits", "blk": "blockedShots", "shg": "shorthandedGoals",
        "sha": "shorthandedAssists", "gwg": "gameWinningGoals",
        "fow": "faceoffWins", "plusMinus": "plusMinus",
    }.items():
        add(row, key, player.get(source))

    ppp = number(player.get("powerPlayPoints"))
    if ppp is None:
        ppg = number(player.get("powerPlayGoals"))
        ppa = number(player.get("powerPlayAssists"))
        ppp = ppg + ppa if ppg is not None and ppa is not None else None
    add(row, "ppp", ppp)

    goals = number(player.get("goals"))
    add(row, "hat", int(goals >= 3) if goals is not None else None)
    add(row, "seconds", toi_seconds(player.get("toi")))


def new_goalie_row(player, team):
    return dict(
        name=player_name(player),
        team=team,
        player_id=player["playerId"],
        **{k: 0 for k in ("gs", "w", "ga", "sv", "so", "otl", "gp", "sa", "seconds")},
    )


def merge_goalie(bucket, player, team, box=None, side_goalies=None, opponent_score=None):
    if played(player) is False:
        return

    row = bucket.setdefault(player["playerId"], new_goalie_row(player, team))
    row["team"] = team
    row["player_id"] = player["playerId"]
    add(row, "gp", 1)
    add(row, "gs", int(player["starter"]) if isinstance(player.get("starter"), bool) else None)

    decision = player.get("decision")
    add(row, "w", int(decision == "W"))
    add(row, "ga", player.get("goalsAgainst"))

    saves = number(player.get("saves"))
    shots = number(player.get("shotsAgainst"))
    goals = number(player.get("goalsAgainst"))
    if saves is None and shots is not None and goals is not None:
        saves = shots - goals

    add(row, "sv", saves)
    add(row, "sa", shots if shots is not None else (
        saves + goals if saves is not None and goals is not None else None
    ))

    duration = toi_seconds(player.get("toi"))
    add(row, "seconds", duration)

    shutout = player.get("shutout")
    if not isinstance(shutout, (bool, int)):
        appearances = [played(g) for g in (side_goalies or [])]
        if opponent_score is not None and appearances and None not in appearances:
            shutout = (
                opponent_score == 0
                and appearances.count(True) == 1
                and played(player) is True
            )
        else:
            shutout = None
    add(row, "so", int(bool(shutout)) if shutout is not None else None)

    period = number((box or {}).get("periodDescriptor", {}).get("number"))
    add(row, "otl", int(decision == "L" and period > 3) if period is not None else None)


def finalize_skaters(rows):
    for p in rows.values():
        gp = number(p.get("gp"))
        seconds = number(p.get("seconds"))
        p["toi"] = format_toi(seconds)
        p["toiPerGame"] = seconds / gp if seconds is not None and gp and gp > 0 else None
        p["avgToi"] = format_toi(p["toiPerGame"])
        for key in (
            "g", "a", "pim", "ppp", "shg", "sha", "gwg", "hat",
            "sog", "hit", "blk", "fow", "plusMinus", "gp", "seconds"
        ):
            p[key] = clean_number(p.get(key))


def finalize_goalies(rows):
    for p in rows.values():
        saves = number(p.get("sv"))
        shots = number(p.get("sa"))
        goals = number(p.get("ga"))
        seconds = number(p.get("seconds"))
        gp = number(p.get("gp"))

        p["svPct"] = saves / shots if saves is not None and shots is not None and shots > 0 else None
        p["gaa"] = goals * 3600 / seconds if goals is not None and seconds is not None and seconds > 0 else None
        p["toi"] = format_toi(seconds)
        p["toiPerGame"] = seconds / gp if seconds is not None and gp and gp > 0 else None
        p["avgToi"] = format_toi(p["toiPerGame"])

        for key in ("gs", "w", "ga", "sv", "so", "otl", "gp", "sa", "seconds"):
            p[key] = clean_number(p.get(key))


def aggregate(boxes, start_date, end_date):
    skaters, goalies = {}, {}
    count = 0

    for box in boxes:
        game_date = date.fromisoformat(box["gameDate"])
        if not start_date <= game_date <= end_date:
            continue

        count += 1
        stats = box.get("playerByGameStats")
        if not isinstance(stats, dict):
            raise ValueError(f"Missing player stats for {box.get('id')}")

        for side, opponent in (("awayTeam", "homeTeam"), ("homeTeam", "awayTeam")):
            group = stats.get(side)
            if not isinstance(group, dict) or not all(
                isinstance(group.get(k), list) for k in ("forwards", "defense", "goalies")
            ):
                raise ValueError(f"Incomplete stats for {box.get('id')}/{side}")

            team = box[side]["abbrev"]
            for player in group["forwards"] + group["defense"]:
                merge_skater(skaters, player, team)

            for player in group["goalies"]:
                merge_goalie(
                    goalies, player, team, box, group["goalies"],
                    number(box[opponent].get("score"))
                )

    finalize_skaters(skaters)
    finalize_goalies(goalies)
    return {
        "game_type": 2,
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "games_count": count,
        "skaters": list(skaters.values()),
        "goalies": list(goalies.values()),
    }


def skater_game_log(player, team, opponent, game_id, game_date, home_away):
    ppp = number(player.get("powerPlayPoints"))
    if ppp is None:
        ppg = number(player.get("powerPlayGoals"))
        ppa = number(player.get("powerPlayAssists"))
        ppp = ppg + ppa if ppg is not None and ppa is not None else None
    goals = number(player.get("goals"))

    return {
        "game_id": int(game_id), "date": game_date, "team": team,
        "opponent": opponent, "home_away": home_away,
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


def goalie_game_log(player, team, opponent, game_id, game_date, home_away,
                    box, side_goalies, opponent_score):
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
        if opponent_score is not None and appearances and None not in appearances:
            shutout = (
                opponent_score == 0
                and appearances.count(True) == 1
                and played(player) is True
            )
        else:
            shutout = None

    period = number((box or {}).get("periodDescriptor", {}).get("number"))

    return {
        "game_id": int(game_id), "date": game_date, "team": team,
        "opponent": opponent, "home_away": home_away,
        "starter": player.get("starter") if isinstance(player.get("starter"), bool) else None,
        "decision": decision,
        "w": int(decision == "W"),
        "otl": int(decision == "L" and period > 3) if period is not None else None,
        "ga": clean_number(goals), "sv": clean_number(saves), "sa": clean_number(shots),
        "svPct": saves / shots if saves is not None and shots is not None and shots > 0 else None,
        "so": int(bool(shutout)) if shutout is not None else None,
        "toi": player.get("toi"), "toi_seconds": duration,
        "gaa": goals * 3600 / duration if goals is not None and duration is not None and duration > 0 else None,
    }


def build_game_logs(boxes):
    skaters = defaultdict(lambda: {"name": None, "team": None, "pos": None, "games": []})
    goalies = defaultdict(lambda: {"name": None, "team": None, "pos": "G", "games": []})

    for box in sorted(boxes, key=lambda b: (b["gameDate"], int(b["id"]))):
        stats = box["playerByGameStats"]
        game_id, game_date = box["id"], box["gameDate"]

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
                entry["name"], entry["team"] = player_name(player), team
                entry["pos"] = player.get("position", "F")
                entry["games"].append(
                    skater_game_log(player, team, opponent, game_id, game_date, home_away)
                )

            for player in group["goalies"]:
                log = goalie_game_log(
                    player, team, opponent, game_id, game_date, home_away,
                    box, group["goalies"], opponent_score
                )
                if log is None:
                    continue
                pid = str(player["playerId"])
                entry = goalies[pid]
                entry["name"], entry["team"] = player_name(player), team
                entry["games"].append(log)

    return {"skaters": dict(skaters), "goalies": dict(goalies)}


def live_game_summary(schedule_game, box):
    period = box.get("periodDescriptor") or {}
    clock = box.get("clock") or {}
    away = box.get("awayTeam") or schedule_game.get("awayTeam") or {}
    home = box.get("homeTeam") or schedule_game.get("homeTeam") or {}

    return {
        "game_id": int(box.get("id") or schedule_game["id"]),
        "game_date": box.get("gameDate") or schedule_game.get("gameDate"),
        "state": box.get("gameState") or schedule_game.get("gameState"),
        "period": clean_number(number(period.get("number"))),
        "period_type": period.get("periodType"),
        "clock": clock.get("timeRemaining"),
        "in_intermission": clock.get("inIntermission"),
        "away": {"abbrev": away.get("abbrev"), "score": clean_number(number(away.get("score")))},
        "home": {"abbrev": home.get("abbrev"), "score": clean_number(number(home.get("score")))},
    }


def build_live_data(schedule_games, boxes_by_id):
    skaters, goalies, games = {}, {}, []

    for schedule_game in schedule_games:
        box = boxes_by_id.get(str(schedule_game["id"]))
        if not box or box.get("gameState") not in LIVE_STATES:
            continue

        stats = box.get("playerByGameStats")
        if not isinstance(stats, dict):
            continue

        games.append(live_game_summary(schedule_game, box))

        for side, opponent_side in (("awayTeam", "homeTeam"), ("homeTeam", "awayTeam")):
            group = stats.get(side)
            if not isinstance(group, dict):
                continue

            forwards = group.get("forwards") or []
            defense = group.get("defense") or []
            side_goalies = group.get("goalies") or []
            team = box[side]["abbrev"]
            opponent_score = number(box[opponent_side].get("score"))

            for player in forwards + defense:
                merge_skater(skaters, player, team)

            for player in side_goalies:
                merge_goalie(
                    goalies, player, team, box, side_goalies, opponent_score
                )

    finalize_skaters(skaters)
    finalize_goalies(goalies)

    return {
        "games_count": len(games),
        "games": games,
        "skaters": list(skaters.values()),
        "goalies": list(goalies.values()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    now_local = datetime.now(LOCAL_ZONE)
    today = now_local.date()

    # Include completed games from today immediately after they become final.
    end_date = today
    week_start = end_date - timedelta(days=DAYS_BACK - 1)
    month_start = end_date - timedelta(days=MONTH_DAYS - 1)
    season_start = season_start_for(end_date)

    completed_ids = completed_game_ids_for_range(season_start, end_date)
    print(f"{season_start} through {end_date}: {len(completed_ids)} completed regular-season games")

    with ThreadPoolExecutor(max_workers=6) as pool:
        completed_boxes = list(pool.map(fetch_boxscore, completed_ids))

    for box in completed_boxes:
        if str(box.get("gameType")) != "2" or box.get("gameState") not in FINAL_STATES:
            raise ValueError("Unexpected completed-game type/state; previous data retained")
        if not season_start <= date.fromisoformat(box["gameDate"]) <= end_date:
            raise ValueError("Out-of-range boxscore; previous data retained")

    data = aggregate(completed_boxes, week_start, end_date)
    data["monthly"] = aggregate(completed_boxes, month_start, end_date)
    data["season"] = aggregate(completed_boxes, season_start, end_date)
    data["game_logs"] = build_game_logs(completed_boxes)

    data["raw_performance"] = {
        "version": 2,
        "season_start": season_start.isoformat(),
        "through": end_date.isoformat(),
        "windows": {"week_days": DAYS_BACK, "month_days": MONTH_DAYS},
        "description": (
            "Completed-game season totals and chronological game logs, "
            "with in-progress games stored separately under live."
        ),
    }

    # Yesterday/today/tomorrow covers games around Toronto midnight.
    live_days = [today - timedelta(days=1), today, today + timedelta(days=1)]
    scheduled_live = live_games_for_dates(live_days)

    live_boxes_by_id = {}
    if scheduled_live:
        live_ids = [str(game["id"]) for game in scheduled_live]
        with ThreadPoolExecutor(max_workers=6) as pool:
            live_boxes = list(pool.map(fetch_boxscore, live_ids))
        live_boxes_by_id = {
            str(box["id"]): box for box in live_boxes if box.get("id") is not None
        }

    data["live"] = build_live_data(scheduled_live, live_boxes_by_id)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["timezone"] = "America/Toronto"
    data["waivers"] = []

    # Preserve manually maintained waiver data.
    if OUT_PATH.exists():
        try:
            old = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            data["waivers"] = old.get("waivers", [])
        except (ValueError, AttributeError):
            pass

    # Atomic write: failed runs leave the previous data.json untouched.
    temp = OUT_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(OUT_PATH)

    print(
        "Wrote Ice Watchers Near Live v1: "
        f"{data['games_count']} completed games in 7-day window, "
        f"{len(data['season']['skaters'])} season skaters, "
        f"{data['live']['games_count']} live games, "
        f"{len(data['live']['skaters'])} live skaters, "
        f"{len(data['live']['goalies'])} live goalies"
    )


if __name__ == "__main__":
    main()
