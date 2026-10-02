"""Ice Watchers NHL data generator: completed regular-season totals + near live.

Python 3.12 standard library only. Keep beside data.json.

Features:
- Last 7 days of completed regular-season stats
- Last 30 days
- Current-season totals
- Previous-season totals for waiver graduation protection
- Individual game logs
- Near-live game stats
- Faceoff wins from play-by-play
- Power-play points from NHL scoring summaries
- Resilient handling of NHL scoring credits for players omitted from
  playerByGameStats

FW is stored as fow.
PPP is stored as ppp.

The NHL API occasionally credits a goal/assist to a player who is not
present in playerByGameStats for that snapshot. Those credits are skipped
for supplemental validation rather than freezing the entire site.
"""

import json
import math
import time
import urllib.request
import urllib.parse

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


NHL_API = "https://api-web.nhle.com/v1"
NHL_STATS_API = "https://api.nhle.com/stats/rest/en"

DAYS_BACK = 7
MONTH_DAYS = 30

OUT_PATH = Path(__file__).resolve().parent / "data.json"

LOCAL_ZONE = ZoneInfo("America/Toronto")

FINAL_STATES = {
    "OFF",
    "FINAL",
}

LIVE_STATES = {
    "LIVE",
    "CRIT",
}

SKATER_KEYS = (
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

GOALIE_KEYS = (
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


# ============================================================
# HTTP
# ============================================================

def get_json(url):

    last_error = None

    for attempt in range(3):

        try:

            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; IceWatch/5.1)",
                    "Accept": "application/json",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )

            with urllib.request.urlopen(
                request,
                timeout=30,
            ) as response:

                value = json.load(response)

            if not isinstance(value, dict):
                raise ValueError(
                    "Expected JSON object: " + url
                )

            return value

        except Exception as exc:

            last_error = exc

            if attempt == 2:
                raise

            time.sleep(attempt + 1)

    raise last_error


# ============================================================
# BASIC HELPERS
# ============================================================

def number(value):

    if isinstance(value, bool) or value is None:
        return None

    try:

        n = float(value)

        return n if math.isfinite(n) else None

    except (TypeError, ValueError):

        return None


def clean_number(value):

    if (
        isinstance(value, float)
        and value.is_integer()
    ):
        return int(value)

    return value


def add(row, key, value):

    n = number(value)

    row[key] = (
        None
        if n is None or row.get(key) is None
        else row[key] + n
    )


def player_name(player):

    name = player.get(
        "name",
        "Unknown",
    )

    if isinstance(name, dict):

        return name.get(
            "default",
            "Unknown",
        )

    return str(name)


def toi_seconds(value):

    if (
        not isinstance(value, str)
        or ":" not in value
    ):
        return None

    try:

        minutes, seconds = map(
            int,
            value.split(":"),
        )

        return minutes * 60 + seconds

    except ValueError:

        return None


def format_toi(seconds):

    if seconds is None:
        return None

    seconds = int(
        round(seconds)
    )

    return (
        f"{seconds // 60}:"
        f"{seconds % 60:02d}"
    )


def played(player):

    seconds = toi_seconds(
        player.get("toi")
    )

    if seconds is None:
        return None

    return seconds > 0


def season_start_for(end_date):

    start_year = (
        end_date.year
        if end_date.month >= 7
        else end_date.year - 1
    )

    return date(
        start_year,
        7,
        1,
    )


def previous_season_id_for(day):

    current_start_year = (
        day.year
        if day.month >= 7
        else day.year - 1
    )

    previous_start_year = (
        current_start_year - 1
    )

    return (
        previous_start_year * 10000
        + previous_start_year
        + 1
    )


# ============================================================
# SCHEDULE
# ============================================================

def schedule_games_for_date(day):

    payload = get_json(
        f"{NHL_API}/schedule/{day.isoformat()}"
    )

    if not isinstance(
        payload.get("gameWeek"),
        list,
    ):
        raise ValueError(
            "Schedule response has no gameWeek array"
        )

    return [
        game
        for entry in payload["gameWeek"]
        for game in entry.get(
            "games",
            [],
        )
        if (
            date.fromisoformat(
                game.get("gameDate")
                or entry["date"]
            )
            == day
            and
            str(
                game.get("gameType")
            )
            == "2"
        )
    ]


def completed_game_ids_for_range(
    start_date,
    end_date,
):

    games = {}

    day = start_date

    while day <= end_date:

        payload = get_json(
            f"{NHL_API}/schedule/{day.isoformat()}"
        )

        entries = payload.get(
            "gameWeek"
        )

        if (
            not isinstance(entries, list)
            or not entries
        ):
            raise ValueError(
                "Schedule response has no gameWeek array"
            )

        last_day = day

        for entry in entries:

            entry_day = date.fromisoformat(
                entry["date"]
            )

            last_day = max(
                last_day,
                entry_day,
            )

            for game in entry.get(
                "games",
                [],
            ):

                game_day = date.fromisoformat(
                    game.get("gameDate")
                    or entry["date"]
                )

                if (
                    start_date
                    <= game_day
                    <= end_date
                    and
                    str(
                        game.get("gameType")
                    )
                    == "2"
                    and
                    game.get("gameState")
                    in FINAL_STATES
                ):

                    games[
                        str(game["id"])
                    ] = game_day

        day = (
            last_day
            + timedelta(days=1)
        )

    return sorted(
        games,
        key=lambda gid: (
            games[gid],
            gid,
        ),
    )


def live_games_for_dates(days):

    games = {}

    for day in days:

        for game in schedule_games_for_date(
            day
        ):

            state = game.get(
                "gameState"
            )

            print(
                f"Schedule game {game.get('id')}: "
                f"state={state}"
            )

            if state in LIVE_STATES:

                games[
                    str(game["id"])
                ] = game

    return list(
        games.values()
    )


# ============================================================
# GAME VALIDATION
# ============================================================

def skaters_in_box(box):

    stats = box.get(
        "playerByGameStats"
    )

    if not isinstance(
        stats,
        dict,
    ):
        raise ValueError(
            f"Missing player stats for {box.get('id')}"
        )

    players = []

    for side in (
        "awayTeam",
        "homeTeam",
    ):

        group = stats.get(side)

        if (
            not isinstance(group, dict)
            or
            not all(
                isinstance(
                    group.get(k),
                    list,
                )
                for k in (
                    "forwards",
                    "defense",
                    "goalies",
                )
            )
        ):
            raise ValueError(
                f"Incomplete stats for "
                f"{box.get('id')}/{side}"
            )

        players.extend(
            group["forwards"]
            + group["defense"]
        )

    return players


# ============================================================
# PPP + FACEOFF SUPPLEMENT
# ============================================================

def supplement_skater_stats(
    box,
    pbp,
    landing,
):

    gid = str(
        box["id"]
    )

    if (
        str(pbp.get("id")) != gid
        or
        str(landing.get("id")) != gid
    ):
        raise ValueError(
            f"Mismatched game feeds: {gid}"
        )

    players = skaters_in_box(
        box
    )

    by_id = {
        int(p["playerId"]): p
        for p in players
    }

    counts = {
        pid: {
            "fow": 0,
            "ppp": 0,
            "shg": 0,
            "sha": 0,
            "goals": 0,
            "assists": 0,
        }
        for pid in by_id
    }

    unknown_credits = set()

    def credit(pid, key):

        if pid is None:

            print(
                f"WARNING: missing {key} player ID "
                f"in game {gid}; skipping credit"
            )

            return False

        try:
            pid = int(pid)

        except (TypeError, ValueError):

            print(
                f"WARNING: invalid {key} player ID "
                f"in game {gid}: {pid!r}; "
                "skipping credit"
            )

            return False

        if pid not in counts:

            marker = (
                pid,
                key,
            )

            if marker not in unknown_credits:

                print(
                    f"WARNING: NHL feed credited {key} "
                    f"to player {pid} in game {gid}, "
                    "but that player is not present in "
                    "playerByGameStats; skipping "
                    "supplemental credit"
                )

                unknown_credits.add(
                    marker
                )

            return False

        counts[
            pid
        ][key] += 1

        return True


    plays = pbp.get(
        "plays"
    )

    scoring = (
        landing.get(
            "summary"
        )
        or {}
    ).get(
        "scoring"
    )

    if (
        not isinstance(plays, list)
        or
        not isinstance(scoring, list)
    ):
        raise ValueError(
            f"Missing play-by-play/scoring feed: {gid}"
        )


    seen = set()
    pbp_goals = 0

    for play in plays:

        if (
            play.get(
                "periodDescriptor",
                {},
            ).get(
                "periodType"
            )
            == "SO"
        ):
            continue

        kind = play.get(
            "typeDescKey"
        )

        if kind not in {
            "faceoff",
            "goal",
        }:
            continue

        event_id = play.get(
            "eventId"
        )

        if event_id is None:

            print(
                f"WARNING: missing event ID "
                f"in game {gid}; skipping event"
            )

            continue

        if event_id in seen:
            continue

        seen.add(
            event_id
        )

        if kind == "faceoff":

            winner = (
                play.get("details")
                or {}
            ).get(
                "winningPlayerId"
            )

            credit(
                winner,
                "fow",
            )

        else:

            pbp_goals += 1


    summary_goals = 0

    seen = set()

    for period in scoring:

        if (
            period.get(
                "periodDescriptor",
                {},
            ).get(
                "periodType"
            )
            == "SO"
        ):
            continue

        goals = period.get(
            "goals"
        )

        if not isinstance(
            goals,
            list,
        ):
            raise ValueError(
                f"Missing summary goal list: {gid}"
            )

        for goal in goals:

            event_id = goal.get(
                "eventId"
            )

            if event_id is None:

                print(
                    f"WARNING: summary goal has no "
                    f"event ID in game {gid}; "
                    "processing anyway"
                )

                event_id = (
                    "summary",
                    summary_goals,
                )

            if event_id in seen:
                continue

            seen.add(
                event_id
            )

            summary_goals += 1

            strength = str(
                goal.get(
                    "strength",
                    "",
                )
            ).lower()

            if strength not in {
                "ev",
                "pp",
                "sh",
            }:

                print(
                    f"WARNING: unknown goal strength "
                    f"{strength!r} in game {gid}; "
                    "goal/assist validation retained "
                    "but special-teams credit skipped"
                )

            scorer = goal.get(
                "playerId"
            )

            assists = goal.get(
                "assists"
            )

            if not isinstance(
                assists,
                list,
            ):

                assists = []

                print(
                    f"WARNING: missing assists list "
                    f"in game {gid}; treating as "
                    "unassisted"
                )

            credit(
                scorer,
                "goals",
            )

            for assist in assists:

                credit(
                    assist.get(
                        "playerId"
                    ),
                    "assists",
                )

            if strength == "pp":

                ids = [
                    scorer
                ]

                ids.extend(
                    assist.get(
                        "playerId"
                    )
                    for assist in assists
                )

                credited = set()

                for pid in ids:

                    try:
                        normalized_pid = int(pid)

                    except (
                        TypeError,
                        ValueError,
                    ):
                        normalized_pid = pid

                    if normalized_pid in credited:
                        continue

                    credited.add(
                        normalized_pid
                    )

                    credit(
                        pid,
                        "ppp",
                    )

            elif strength == "sh":

                credit(
                    scorer,
                    "shg",
                )

                for assist in assists:

                    credit(
                        assist.get(
                            "playerId"
                        ),
                        "sha",
                    )


    if summary_goals != pbp_goals:

        print(
            f"WARNING: scoring/play-by-play "
            f"goal totals disagree for game {gid}: "
            f"summary={summary_goals}, "
            f"pbp={pbp_goals}. "
            "Using boxscore goals and scoring-summary "
            "special-teams credits."
        )


    if (
        box.get("gameState")
        in FINAL_STATES
        and
        not any(
            c["fow"]
            for c in counts.values()
        )
    ):

        print(
            f"WARNING: completed game {gid} "
            "has no usable faceoff-win events"
        )


    for pid, player in by_id.items():

        c = counts[pid]

        box_goals = number(
            player.get("goals")
        )

        box_assists = number(
            player.get("assists")
        )

        if (
            box_goals is not None
            and
            box_goals != c["goals"]
        ):

            print(
                f"WARNING: goal-credit snapshot "
                f"differs for game {gid}, "
                f"player {pid}: "
                f"box={box_goals}, "
                f"summary={c['goals']}. "
                "Keeping official boxscore goals."
            )

        if (
            box_assists is not None
            and
            box_assists != c["assists"]
        ):

            print(
                f"WARNING: assist-credit snapshot "
                f"differs for game {gid}, "
                f"player {pid}: "
                f"box={box_assists}, "
                f"summary={c['assists']}. "
                "Keeping official boxscore assists."
            )

        player[
            "faceoffWins"
        ] = c["fow"]

        player[
            "powerPlayPoints"
        ] = c["ppp"]

        player[
            "shorthandedGoals"
        ] = c["shg"]

        player[
            "shorthandedAssists"
        ] = c["sha"]


    return box


def fetch_boxscore(game_id):

    game_id = str(
        game_id
    )

    last_error = None

    for attempt in range(5):

        try:

            print(
                f"Fetching fresh game {game_id} "
                f"(attempt {attempt + 1}/5)"
            )

            cache_buster = int(
                time.time() * 1000
            )

            box = get_json(
                f"{NHL_API}/gamecenter/"
                f"{game_id}/boxscore"
                f"?_={cache_buster}"
            )

            pbp = get_json(
                f"{NHL_API}/gamecenter/"
                f"{game_id}/play-by-play"
                f"?_={cache_buster}"
            )

            landing = get_json(
                f"{NHL_API}/gamecenter/"
                f"{game_id}/landing"
                f"?_={cache_buster}"
            )

            result = supplement_skater_stats(
                box,
                pbp,
                landing,
            )

            print(
                f"Game {game_id} fetched successfully "
                f"(state={result.get('gameState')})"
            )

            return result

        except Exception as exc:

            last_error = exc

            print(
                f"WARNING: game {game_id} "
                f"attempt {attempt + 1}/5 failed: "
                f"{type(exc).__name__}: {exc}"
            )

            if attempt < 4:

                time.sleep(
                    2 ** attempt
                )

    raise RuntimeError(
        f"Unable to build validated stats "
        f"for NHL game {game_id} after 5 attempts. "
        f"Last error: "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


# ============================================================
# PLAYER AGGREGATION
# ============================================================

def new_skater_row(
    player,
    team,
):

    return dict(
        name=player_name(
            player
        ),
        team=team,
        pos=player.get(
            "position",
            "F",
        ),
        player_id=player[
            "playerId"
        ],
        **{
            k: 0
            for k in SKATER_KEYS
        },
    )


def merge_skater(
    bucket,
    player,
    team,
):

    row = bucket.setdefault(
        player["playerId"],
        new_skater_row(
            player,
            team,
        ),
    )

    row.update(
        team=team,
        player_id=player[
            "playerId"
        ],
        pos=player.get(
            "position",
            row["pos"],
        ),
    )

    add(
        row,
        "gp",
        1,
    )

    sources = {
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
        "ppp": "powerPlayPoints",
    }

    for key, source in sources.items():

        add(
            row,
            key,
            player.get(source),
        )

    goals = number(
        player.get("goals")
    )

    add(
        row,
        "hat",
        int(
            goals >= 3
        )
        if goals is not None
        else None,
    )

    add(
        row,
        "seconds",
        toi_seconds(
            player.get("toi")
        ),
    )


# ============================================================
# GOALIES
# ============================================================

def goalie_values(
    player,
    box,
    side_goalies,
    opponent_score,
):

    saves, shots, goals = (
        number(
            player.get(k)
        )
        for k in (
            "saves",
            "shotsAgainst",
            "goalsAgainst",
        )
    )

    if (
        saves is None
        and shots is not None
        and goals is not None
    ):
        saves = (
            shots - goals
        )

    if (
        shots is None
        and saves is not None
        and goals is not None
    ):
        shots = (
            saves + goals
        )

    duration = toi_seconds(
        player.get("toi")
    )

    decision = player.get(
        "decision"
    )

    shutout = player.get(
        "shutout"
    )

    if not isinstance(
        shutout,
        (bool, int),
    ):

        appearances = [
            played(g)
            for g in (
                side_goalies
                or []
            )
        ]

        shutout = (
            opponent_score == 0
            and appearances.count(True) == 1
            and played(player) is True

            if (
                opponent_score is not None
                and appearances
                and None not in appearances
            )

            else None
        )

    period = number(
        (
            box
            or {}
        ).get(
            "periodDescriptor",
            {},
        ).get(
            "number"
        )
    )

    return dict(
        w=int(
            decision == "W"
        ),
        ga=goals,
        sv=saves,
        sa=shots,
        so=(
            int(
                bool(shutout)
            )
            if shutout is not None
            else None
        ),
        otl=(
            int(
                decision == "L"
                and period > 3
            )
            if period is not None
            else None
        ),
        seconds=duration,
        gs=(
            int(
                player["starter"]
            )
            if isinstance(
                player.get("starter"),
                bool,
            )
            else None
        ),
    )


def merge_goalie(
    bucket,
    player,
    team,
    box=None,
    side_goalies=None,
    opponent_score=None,
):

    if played(player) is False:
        return

    row = bucket.setdefault(
        player["playerId"],
        dict(
            name=player_name(
                player
            ),
            team=team,
            player_id=player[
                "playerId"
            ],
            **{
                k: 0
                for k in GOALIE_KEYS
            },
        ),
    )

    row.update(
        team=team,
        player_id=player[
            "playerId"
        ],
    )

    add(
        row,
        "gp",
        1,
    )

    for key, value in goalie_values(
        player,
        box,
        side_goalies,
        opponent_score,
    ).items():

        add(
            row,
            key,
            value,
        )


# ============================================================
# FINALIZE
# ============================================================

def finalize_skaters(rows):

    for p in rows.values():

        gp = number(
            p.get("gp")
        )

        seconds = number(
            p.get("seconds")
        )

        p["toi"] = format_toi(
            seconds
        )

        p["toiPerGame"] = (
            seconds / gp

            if (
                seconds is not None
                and gp
                and gp > 0
            )

            else None
        )

        p["avgToi"] = format_toi(
            p["toiPerGame"]
        )

        for key in SKATER_KEYS:

            p[key] = clean_number(
                p.get(key)
            )


def finalize_goalies(rows):

    for p in rows.values():

        saves, shots, goals, seconds, gp = (
            number(
                p.get(k)
            )
            for k in (
                "sv",
                "sa",
                "ga",
                "seconds",
                "gp",
            )
        )

        p["svPct"] = (
            saves / shots

            if (
                saves is not None
                and shots is not None
                and shots > 0
            )

            else None
        )

        p["gaa"] = (
            goals * 3600 / seconds

            if (
                goals is not None
                and seconds is not None
                and seconds > 0
            )

            else None
        )

        p["toi"] = format_toi(
            seconds
        )

        p["toiPerGame"] = (
            seconds / gp

            if (
                seconds is not None
                and gp
                and gp > 0
            )

            else None
        )

        p["avgToi"] = format_toi(
            p["toiPerGame"]
        )

        for key in GOALIE_KEYS:

            p[key] = clean_number(
                p.get(key)
            )


# ============================================================
# MERGE GAME
# ============================================================

def merge_box(
    box,
    skaters,
    goalies,
):

    skaters_in_box(
        box
    )

    for side, opponent in (
        (
            "awayTeam",
            "homeTeam",
        ),
        (
            "homeTeam",
            "awayTeam",
        ),
    ):

        group = box[
            "playerByGameStats"
        ][side]

        team = box[
            side
        ]["abbrev"]

        for player in (
            group["forwards"]
            + group["defense"]
        ):

            merge_skater(
                skaters,
                player,
                team,
            )

        for player in group[
            "goalies"
        ]:

            merge_goalie(
                goalies,
                player,
                team,
                box,
                group["goalies"],
                number(
                    box[
                        opponent
                    ].get(
                        "score"
                    )
                ),
            )


# ============================================================
# PERIOD AGGREGATION
# ============================================================

def aggregate(
    boxes,
    start_date,
    end_date,
):

    skaters = {}
    goalies = {}
    count = 0

    for box in boxes:

        game_date = date.fromisoformat(
            box["gameDate"]
        )

        if (
            start_date
            <= game_date
            <= end_date
        ):

            count += 1

            merge_box(
                box,
                skaters,
                goalies,
            )

    finalize_skaters(
        skaters
    )

    finalize_goalies(
        goalies
    )

    return dict(
        game_type=2,
        period_start=start_date.isoformat(),
        period_end=end_date.isoformat(),
        games_count=count,
        skaters=list(
            skaters.values()
        ),
        goalies=list(
            goalies.values()
        ),
    )


# ============================================================
# GAME LOGS
# ============================================================

def skater_game_log(
    player,
    team,
    opponent,
    game_id,
    game_date,
    home_away,
):

    row = new_skater_row(
        player,
        team,
    )

    bucket = {
        player["playerId"]: row
    }

    merge_skater(
        bucket,
        player,
        team,
    )

    finalize_skaters(
        bucket
    )

    result = {
        k: row[k]
        for k in SKATER_KEYS
        if k not in {
            "gp",
            "seconds",
        }
    }

    result.update(
        game_id=int(
            game_id
        ),
        date=game_date,
        team=team,
        opponent=opponent,
        home_away=home_away,
        pos=player.get(
            "position",
            "F",
        ),
        toi=player.get(
            "toi"
        ),
        toi_seconds=toi_seconds(
            player.get("toi")
        ),
    )

    return result


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

    values = goalie_values(
        player,
        box,
        side_goalies,
        opponent_score,
    )

    row = dict(
        values,
        gp=1,
    )

    finalize_goalies(
        {
            player[
                "playerId"
            ]: row
        }
    )

    result = {
        k: row[k]
        for k in (
            "w",
            "otl",
            "ga",
            "sv",
            "sa",
            "svPct",
            "so",
            "gaa",
        )
    }

    result.update(
        game_id=int(
            game_id
        ),
        date=game_date,
        team=team,
        opponent=opponent,
        home_away=home_away,
        starter=(
            player.get(
                "starter"
            )
            if isinstance(
                player.get(
                    "starter"
                ),
                bool,
            )
            else None
        ),
        decision=player.get(
            "decision"
        ),
        toi=player.get(
            "toi"
        ),
        toi_seconds=values[
            "seconds"
        ],
    )

    return result


def build_game_logs(
    boxes,
):

    skaters = defaultdict(
        lambda: dict(
            name=None,
            team=None,
            pos=None,
            games=[],
        )
    )

    goalies = defaultdict(
        lambda: dict(
            name=None,
            team=None,
            pos="G",
            games=[],
        )
    )

    for box in sorted(
        boxes,
        key=lambda b: (
            b["gameDate"],
            int(
                b["id"]
            ),
        ),
    ):

        for (
            side,
            opponent_side,
            home_away,
        ) in (
            (
                "awayTeam",
                "homeTeam",
                "away",
            ),
            (
                "homeTeam",
                "awayTeam",
                "home",
            ),
        ):

            group = box[
                "playerByGameStats"
            ][side]

            team = box[
                side
            ]["abbrev"]

            opponent = box[
                opponent_side
            ]["abbrev"]

            for player in (
                group["forwards"]
                + group["defense"]
            ):

                entry = skaters[
                    str(
                        player[
                            "playerId"
                        ]
                    )
                ]

                entry.update(
                    name=player_name(
                        player
                    ),
                    team=team,
                    pos=player.get(
                        "position",
                        "F",
                    ),
                )

                entry[
                    "games"
                ].append(
                    skater_game_log(
                        player,
                        team,
                        opponent,
                        box["id"],
                        box["gameDate"],
                        home_away,
                    )
                )

            for player in group[
                "goalies"
            ]:

                log = goalie_game_log(
                    player,
                    team,
                    opponent,
                    box["id"],
                    box["gameDate"],
                    home_away,
                    box,
                    group["goalies"],
                    number(
                        box[
                            opponent_side
                        ].get(
                            "score"
                        )
                    ),
                )

                if log is not None:

                    entry = goalies[
                        str(
                            player[
                                "playerId"
                            ]
                        )
                    ]

                    entry.update(
                        name=player_name(
                            player
                        ),
                        team=team,
                    )

                    entry[
                        "games"
                    ].append(
                        log
                    )

    return dict(
        skaters=dict(
            skaters
        ),
        goalies=dict(
            goalies
        ),
    )


# ============================================================
# LIVE DATA
# ============================================================

def live_game_summary(
    schedule_game,
    box,
):

    period = (
        box.get(
            "periodDescriptor"
        )
        or {}
    )

    clock = (
        box.get(
            "clock"
        )
        or {}
    )

    result = dict(
        game_id=int(
            box.get(
                "id"
            )
            or schedule_game[
                "id"
            ]
        ),
        game_date=(
            box.get(
                "gameDate"
            )
            or schedule_game.get(
                "gameDate"
            )
        ),
        state=(
            box.get(
                "gameState"
            )
            or schedule_game.get(
                "gameState"
            )
        ),
        period=clean_number(
            number(
                period.get(
                    "number"
                )
            )
        ),
        period_type=period.get(
            "periodType"
        ),
        clock=clock.get(
            "timeRemaining"
        ),
        in_intermission=clock.get(
            "inIntermission"
        ),
    )

    for side, label in (
        (
            "awayTeam",
            "away",
        ),
        (
            "homeTeam",
            "home",
        ),
    ):

        team = (
            box.get(side)
            or schedule_game.get(side)
            or {}
        )

        result[label] = dict(
            abbrev=team.get(
                "abbrev"
            ),
            score=clean_number(
                number(
                    team.get(
                        "score"
                    )
                )
            ),
        )

    return result


def build_live_data(
    schedule_games,
    boxes_by_id,
):

    skaters = {}
    goalies = {}
    games = []

    for schedule_game in schedule_games:

        game_id = str(
            schedule_game[
                "id"
            ]
        )

        box = boxes_by_id.get(
            game_id
        )

        if not box:
            continue

        actual_state = box.get(
            "gameState"
        )

        if actual_state not in LIVE_STATES:

            print(
                f"Removing game {game_id} from live: "
                f"fresh boxscore state={actual_state}"
            )

            continue

        games.append(
            live_game_summary(
                schedule_game,
                box,
            )
        )

        merge_box(
            box,
            skaters,
            goalies,
        )

    finalize_skaters(
        skaters
    )

    finalize_goalies(
        goalies
    )

    return dict(
        games_count=len(
            games
        ),
        games=games,
        skaters=list(
            skaters.values()
        ),
        goalies=list(
            goalies.values()
        ),
        updated_at=datetime.now(
            timezone.utc
        ).isoformat(),
    )


# ============================================================
# PREVIOUS SEASON
# ============================================================

def stats_report(
    report_path,
    season_id,
):

    cayenne = (
        f"seasonId={season_id} "
        "and gameTypeId=2"
    )

    query = urllib.parse.urlencode(
        {
            "cayenneExp": cayenne,
            "limit": -1,
            "start": 0,
        }
    )

    url = (
        f"{NHL_STATS_API}/"
        f"{report_path}"
        f"?{query}"
    )

    payload = get_json(
        url
    )

    rows = payload.get(
        "data"
    )

    if not isinstance(
        rows,
        list,
    ):
        raise ValueError(
            "NHL Stats response missing "
            f"data array: {report_path}"
        )

    return rows


def build_previous_season_data(
    today,
):

    season_id = (
        previous_season_id_for(
            today
        )
    )

    print(
        "Loading previous-season "
        f"statistics: {season_id}"
    )

    skater_rows = stats_report(
        "skater/summary",
        season_id,
    )

    goalie_rows = stats_report(
        "goalie/summary",
        season_id,
    )

    skaters = []

    for row in skater_rows:

        player_id = row.get(
            "playerId"
        )

        if player_id is None:
            continue

        goals = clean_number(
            number(
                row.get(
                    "goals"
                )
            )
        )

        assists = clean_number(
            number(
                row.get(
                    "assists"
                )
            )
        )

        points_value = clean_number(
            number(
                row.get(
                    "points"
                )
            )
        )

        if points_value is None:

            if (
                goals is not None
                and assists is not None
            ):
                points_value = (
                    goals + assists
                )

        skaters.append(
            {
                "player_id": player_id,

                "name": (
                    row.get(
                        "skaterFullName"
                    )
                    or
                    row.get(
                        "playerName"
                    )
                    or
                    "Unknown"
                ),

                "team": (
                    row.get(
                        "teamAbbrevs"
                    )
                    or "-"
                ),

                "gp": clean_number(
                    number(
                        row.get(
                            "gamesPlayed"
                        )
                    )
                ),

                "g": goals,

                "a": assists,

                "points": points_value,

                "ppp": clean_number(
                    number(
                        row.get(
                            "ppPoints"
                        )
                    )
                ),

                "sog": clean_number(
                    number(
                        row.get(
                            "shots"
                        )
                    )
                ),
            }
        )


    goalies = []

    for row in goalie_rows:

        player_id = row.get(
            "playerId"
        )

        if player_id is None:
            continue

        goalies.append(
            {
                "player_id": player_id,

                "name": (
                    row.get(
                        "goalieFullName"
                    )
                    or
                    row.get(
                        "playerName"
                    )
                    or
                    "Unknown"
                ),

                "team": (
                    row.get(
                        "teamAbbrevs"
                    )
                    or "-"
                ),

                "gp": clean_number(
                    number(
                        row.get(
                            "gamesPlayed"
                        )
                    )
                ),

                "gs": clean_number(
                    number(
                        row.get(
                            "gamesStarted"
                        )
                    )
                ),

                "w": clean_number(
                    number(
                        row.get(
                            "wins"
                        )
                    )
                ),

                "sv": clean_number(
                    number(
                        row.get(
                            "saves"
                        )
                    )
                ),

                "svPct": number(
                    row.get(
                        "savePct"
                    )
                ),

                "gaa": number(
                    row.get(
                        "goalsAgainstAverage"
                    )
                ),

                "so": clean_number(
                    number(
                        row.get(
                            "shutouts"
                        )
                    )
                ),

                "otl": clean_number(
                    number(
                        row.get(
                            "otLosses"
                        )
                    )
                ),
            }
        )


    print(
        "Previous season loaded: "
        f"{len(skaters)} skaters, "
        f"{len(goalies)} goalies"
    )

    return {
        "season_id": season_id,
        "skaters": skaters,
        "goalies": goalies,
    }


# ============================================================
# MAIN
# ============================================================

def main():

    now_utc = datetime.now(
        timezone.utc
    )

    now_local = datetime.now(
        LOCAL_ZONE
    )

    today = now_local.date()

    print(
        "=" * 48
    )

    print(
        "ICE WATCHERS REFRESH START"
    )

    print(
        f"UTC: {now_utc.isoformat()}"
    )

    print(
        f"Toronto date: {today.isoformat()}"
    )

    print(
        "=" * 48
    )


    week_start = (
        today
        - timedelta(
            days=DAYS_BACK - 1
        )
    )

    month_start = (
        today
        - timedelta(
            days=MONTH_DAYS - 1
        )
    )

    season_start = (
        season_start_for(
            today
        )
    )


    # --------------------------------------------------------
    # FIRST: CHECK LIVE/RECENT GAMES
    #
    # This happens before the season aggregate so games that
    # just changed from LIVE/CRIT to FINAL/OFF can be included
    # immediately in this same refresh.
    # --------------------------------------------------------

    recent_schedule_days = [
        today - timedelta(days=1),
        today,
        today + timedelta(days=1),
    ]

    scheduled_live = (
        live_games_for_dates(
            recent_schedule_days
        )
    )


    # --------------------------------------------------------
    # COMPLETED GAMES
    # --------------------------------------------------------

    completed_ids = (
        completed_game_ids_for_range(
            season_start,
            today,
        )
    )

    print(
        f"{season_start} through "
        f"{today}: "
        f"{len(completed_ids)} "
        "completed regular-season games"
    )


    with ThreadPoolExecutor(
        max_workers=6
    ) as pool:

        completed_boxes = list(
            pool.map(
                fetch_boxscore,
                completed_ids,
            )
        )


    completed_by_id = {
        str(
            box["id"]
        ): box
        for box in completed_boxes
    }


    for box in completed_boxes:

        if (
            str(
                box.get(
                    "gameType"
                )
            )
            != "2"

            or

            box.get(
                "gameState"
            )
            not in FINAL_STATES

            or

            not (
                season_start
                <= date.fromisoformat(
                    box[
                        "gameDate"
                    ]
                )
                <= today
            )
        ):

            raise ValueError(
                "Unexpected completed game; "
                "previous data retained"
            )


    # --------------------------------------------------------
    # FETCH FRESH BOXES FOR GAMES THE SCHEDULE CURRENTLY SAYS
    # ARE LIVE.
    # --------------------------------------------------------

    live_boxes = []

    if scheduled_live:

        with ThreadPoolExecutor(
            max_workers=6
        ) as pool:

            live_boxes = list(
                pool.map(
                    fetch_boxscore,
                    [
                        str(
                            g["id"]
                        )
                        for g in scheduled_live
                    ],
                )
            )


    # --------------------------------------------------------
    # A GAME MAY HAVE FINISHED BETWEEN THE SCHEDULE REQUEST
    # AND THE BOXSCORE REQUEST.
    #
    # If that happens, immediately graduate it from LIVE to
    # completed so its stats are included in the same run.
    # --------------------------------------------------------

    for box in live_boxes:

        game_id = str(
            box["id"]
        )

        state = box.get(
            "gameState"
        )

        print(
            f"Fresh game {game_id}: "
            f"state={state}"
        )

        if state in FINAL_STATES:

            game_day = date.fromisoformat(
                box[
                    "gameDate"
                ]
            )

            if (
                str(
                    box.get(
                        "gameType"
                    )
                )
                == "2"

                and

                season_start
                <= game_day
                <= today
            ):

                completed_by_id[
                    game_id
                ] = box

                print(
                    f"Game {game_id} graduated "
                    "from live to completed"
                )


    completed_boxes = sorted(
        completed_by_id.values(),
        key=lambda b: (
            b["gameDate"],
            int(
                b["id"]
            ),
        ),
    )


    # --------------------------------------------------------
    # BUILD COMPLETED-GAME WINDOWS AFTER LIVE GRADUATION
    # --------------------------------------------------------

    data = aggregate(
        completed_boxes,
        week_start,
        today,
    )

    data[
        "monthly"
    ] = aggregate(
        completed_boxes,
        month_start,
        today,
    )

    data[
        "season"
    ] = aggregate(
        completed_boxes,
        season_start,
        today,
    )

    data[
        "game_logs"
    ] = build_game_logs(
        completed_boxes
    )


    # --------------------------------------------------------
    # BUILD LIVE SECTION
    #
    # build_live_data checks the FRESH boxscore state.
    # A game only remains here if its current boxscore is
    # actually LIVE or CRIT.
    # --------------------------------------------------------

    data[
        "live"
    ] = build_live_data(
        scheduled_live,
        {
            str(
                b["id"]
            ): b
            for b in live_boxes
        },
    )


    print(
        f"Current live games after fresh validation: "
        f"{data['live']['games_count']}"
    )


    # --------------------------------------------------------
    # LOAD OLD DATA BEFORE PREVIOUS-SEASON CACHE
    # --------------------------------------------------------

    old_data = {}

    if OUT_PATH.exists():

        try:

            old_data = json.loads(
                OUT_PATH.read_text(
                    encoding="utf-8"
                )
            )

            if not isinstance(
                old_data,
                dict,
            ):
                old_data = {}

        except (
            ValueError,
            AttributeError,
            OSError,
        ):

            old_data = {}


    # --------------------------------------------------------
    # PREVIOUS SEASON
    #
    # This powers the waiver "graduation" system.
    #
    # We cache it because last season's totals do not need
    # to be downloaded every time the workflow runs.
    # --------------------------------------------------------

    previous_id = (
        previous_season_id_for(
            today
        )
    )

    cached_previous = (
        old_data.get(
            "previous_season"
        )
    )

    if (
        isinstance(
            cached_previous,
            dict,
        )

        and

        cached_previous.get(
            "season_id"
        )
        == previous_id

        and

        isinstance(
            cached_previous.get(
                "skaters"
            ),
            list,
        )

        and

        isinstance(
            cached_previous.get(
                "goalies"
            ),
            list,
        )
    ):

        data[
            "previous_season"
        ] = cached_previous

        print(
            "Using cached previous-season "
            f"statistics: {previous_id}"
        )

    else:

        data[
            "previous_season"
        ] = (
            build_previous_season_data(
                today
            )
        )


    # --------------------------------------------------------
    # METADATA
    # --------------------------------------------------------

    data[
        "raw_performance"
    ] = {

        "version": 6,

        "season_start":
            season_start.isoformat(),

        "through":
            today.isoformat(),

        "windows": {
            "week_days":
                DAYS_BACK,

            "month_days":
                MONTH_DAYS,
        },

        "previous_season": {

            "season_id":
                previous_id,

            "purpose":
                "waiver graduation protection",

            "graduation_rules": {

                "skater_points":
                    60,

                "skater_goals":
                    30,

                "skater_power_play_points":
                    20,

                "goalie_previous_season_top_wins":
                    20,
            },
        },

        "description": (
            "Completed-game season totals and "
            "chronological game logs, with "
            "in-progress games stored separately "
            "under live. Faceoff wins come from "
            "play-by-play and power-play points "
            "from NHL scoring-summary credits. "
            "Previous-season totals support "
            "Ice Watchers waiver graduation. "
            "NHL scoring credits for players "
            "missing from playerByGameStats are "
            "logged and skipped instead of "
            "blocking the refresh."
        ),
    }


    # --------------------------------------------------------
    # PRESERVE MANUAL WAIVER DATA
    # --------------------------------------------------------

    data[
        "waivers"
    ] = old_data.get(
        "waivers",
        [],
    )


    data.update(

        updated_at=datetime.now(
            timezone.utc
        ).isoformat(),

        timezone="America/Toronto",

    )


    # --------------------------------------------------------
    # WRITE SAFELY
    # --------------------------------------------------------

    temp = OUT_PATH.with_suffix(
        ".json.tmp"
    )

    temp.write_text(
        json.dumps(
            data,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )

    temp.replace(
        OUT_PATH
    )


    print(
        "=" * 48
    )

    print(
        "ICE WATCHERS REFRESH COMPLETE"
    )

    print(
        "Wrote Ice Watchers v6: "
        f"{data['games_count']} "
        "completed games in 7-day window, "
        f"{len(data['season']['skaters'])} "
        "season skaters, "
        f"{data['live']['games_count']} "
        "live games, "
        f"{len(data['previous_season']['skaters'])} "
        "previous-season skaters"
    )

    print(
        f"data.json updated at "
        f"{data['updated_at']}"
    )

    print(
        "=" * 48
    )


if __name__ == "__main__":
    main()
