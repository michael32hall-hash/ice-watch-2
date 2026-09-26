"""Aggregate completed regular-season games over rolling 7- and 30-day windows.

At the nightly 1 a.m. run, the window ends yesterday. Games still in progress
are picked up on the next run. NHL gameType 2 denotes the regular season.
Keep every player in data.json; the browser selects the top 10 per scoring rules.
The unofficial NHL API can change. Missing stats are null, never fabricated 0s.
Any failed request aborts the update and preserves the previous data.json.
Uses only Python 3.12's standard library.
"""
import json
import math
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NHL_API = 'https://api-web.nhle.com/v1'
DAYS_BACK = 7
MONTH_DAYS = 30
OUT_PATH = Path(__file__).resolve().parent / 'data.json'
LOCAL_ZONE = ZoneInfo('America/Toronto')


def get_json(url):
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'IceWatch/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(request, timeout=30) as response:
                value = json.load(response)
            if not isinstance(value, dict):
                raise ValueError('Expected a JSON object: ' + url)
            return value
        except Exception:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def get_game_ids_for_range(days_back, end_date=None):
    end_date = end_date or datetime.now(LOCAL_ZONE).date() - timedelta(days=1)
    start_date = end_date - timedelta(days=days_back - 1)
    games = {}
    for offset in range(days_back):
        day = start_date + timedelta(days=offset)
        payload = get_json(f'{NHL_API}/schedule/{day.isoformat()}')
        if not isinstance(payload.get('gameWeek'), list):
            raise ValueError('Schedule response has no gameWeek array')
        # Schedule responses can include a whole week. Filter every returned date.
        for entry in payload['gameWeek']:
            for game in entry.get('games', []):
                game_day = date.fromisoformat(game.get('gameDate') or entry['date'])
                if (start_date <= game_day <= end_date
                        and str(game.get('gameType')) == '2'
                        and game.get('gameState') in ('OFF', 'FINAL')):
                    games[str(game['id'])] = game_day
    return sorted(games, key=lambda gid: (games[gid], gid))


def fetch_boxscore(game_id):
    return get_json(f'{NHL_API}/gamecenter/{game_id}/boxscore')


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def add(row, key, value):
    n = number(value)
    # If one appearance lacks this stat, the whole period's total is unknown.
    row[key] = None if n is None or row.get(key) is None else row[key] + n


def player_name(p):
    name = p.get('name', 'Unknown')
    return name.get('default', 'Unknown') if isinstance(name, dict) else str(name)


def merge_skater(bucket, p, team_abbrev):
    row = bucket.setdefault(p['playerId'], dict(
        name=player_name(p), team=team_abbrev, pos=p.get('position', 'F'),
        **{key: 0 for key in ('g','a','pim','ppp','shg','sha','gwg','hat','sog','hit','blk','fow','plusMinus','gp')}))
    row['team'] = team_abbrev
    row['player_id'] = p['playerId']
    add(row, 'gp', 1)
    for key, source in {'g':'goals','a':'assists','pim':'pim','sog':'sog',
                        'hit':'hits','blk':'blockedShots','shg':'shorthandedGoals',
                        'sha':'shorthandedAssists','gwg':'gameWinningGoals',
                        'fow':'faceoffWins','plusMinus':'plusMinus'}.items():
        add(row, key, p.get(source))
    ppp = number(p.get('powerPlayPoints'))
    if ppp is None:
        goals, assists = number(p.get('powerPlayGoals')), number(p.get('powerPlayAssists'))
        ppp = goals + assists if goals is not None and assists is not None else None
    add(row, 'ppp', ppp)
    goals = number(p.get('goals'))
    add(row, 'hat', int(goals >= 3) if goals is not None else None)


def played(p):
    toi = p.get('toi')
    if not isinstance(toi, str) or ':' not in toi:
        return None
    try:
        minutes, seconds = map(int, toi.split(':'))
        return minutes * 60 + seconds > 0
    except ValueError:
        return None


def merge_goalie(bucket, p, team_abbrev, box=None, side_goalies=None, opponent_score=None):
    if played(p) is False:
        return  # Do not rank unused backups.
    row = bucket.setdefault(p['playerId'], dict(
        name=player_name(p), team=team_abbrev,
        **{key: 0 for key in ('gs','w','ga','sv','so','otl','gp','sa','seconds')}))
    row['team'] = team_abbrev
    row['player_id'] = p['playerId']
    add(row, 'gp', 1)
    add(row, 'gs', int(p['starter']) if isinstance(p.get('starter'), bool) else None)
    decision = p.get('decision')
    add(row, 'w', int(decision == 'W'))
    add(row, 'ga', p.get('goalsAgainst'))
    saves = number(p.get('saves'))
    shots, goals = number(p.get('shotsAgainst')), number(p.get('goalsAgainst'))
    if saves is None and shots is not None and goals is not None:
        saves = shots - goals
    add(row, 'sv', saves)
    add(row, 'sa', shots if shots is not None else (saves + goals if saves is not None and goals is not None else None))
    try:
        minutes, seconds = map(int, p.get('toi', '').split(':'))
        duration = minutes * 60 + seconds
    except (ValueError, AttributeError):
        duration = None
    add(row, 'seconds', duration)
    shutout = p.get('shutout')
    if not isinstance(shutout, (bool, int)):
        appearances = [played(g) for g in (side_goalies or [])]
        if opponent_score is not None and appearances and None not in appearances:
            shutout = (opponent_score == 0 and appearances.count(True) == 1 and played(p) is True)
        else:
            shutout = None
    add(row, 'so', int(bool(shutout)) if shutout is not None else None)
    period = number((box or {}).get('periodDescriptor', {}).get('number'))
    add(row, 'otl', int(decision == 'L' and period > 3) if period is not None else None)


def aggregate(boxes, start_date, end_date):
    skaters, goalies = {}, {}
    count = 0
    for box in boxes:
        if not start_date <= date.fromisoformat(box['gameDate']) <= end_date:
            continue
        count += 1
        stats = box.get('playerByGameStats')
        if not isinstance(stats, dict):
            raise ValueError(f"Missing player statistics for {box.get('id')}")
        for side, opponent in (('awayTeam','homeTeam'), ('homeTeam','awayTeam')):
            group = stats.get(side)
            if not isinstance(group, dict) or not all(isinstance(group.get(k), list) for k in ('forwards','defense','goalies')):
                raise ValueError(f"Incomplete statistics for {box.get('id')}/{side}")
            team = box[side]['abbrev']
            for player in group['forwards'] + group['defense']:
                merge_skater(skaters, player, team)
            for player in group['goalies']:
                merge_goalie(goalies, player, team, box, group['goalies'], number(box[opponent].get('score')))
    for player in goalies.values():
        saves, shots, goals, seconds = (player[k] for k in ('sv', 'sa', 'ga', 'seconds'))
        player['svPct'] = saves / shots if saves is not None and shots is not None and shots > 0 else None
        player['gaa'] = goals * 3600 / seconds if goals is not None and seconds is not None and seconds > 0 else None
    return dict(game_type=2, period_start=start_date.isoformat(), period_end=end_date.isoformat(),
                games_count=count, skaters=list(skaters.values()), goalies=list(goalies.values()))


def main():
    end_date = datetime.now(LOCAL_ZONE).date() - timedelta(days=1)
    week_start = end_date - timedelta(days=DAYS_BACK - 1)
    month_start = end_date - timedelta(days=MONTH_DAYS - 1)
    ids = get_game_ids_for_range(MONTH_DAYS, end_date)
    print(f'{month_start} through {end_date}: {len(ids)} completed regular-season games')
    # Fetch each game once, then use it in either or both windows.
    with ThreadPoolExecutor(max_workers=6) as pool:
        boxes = list(pool.map(fetch_boxscore, ids))
    for box in boxes:
        if str(box.get('gameType')) != '2' or box.get('gameState') not in ('OFF','FINAL'):
            raise ValueError('Unexpected game type/state; previous data retained')
        if not month_start <= date.fromisoformat(box['gameDate']) <= end_date:
            raise ValueError('Out-of-range boxscore; previous data retained')
    data = aggregate(boxes, week_start, end_date)
    data['monthly'] = aggregate(boxes, month_start, end_date)
    data['updated_at'] = datetime.now(timezone.utc).isoformat()
    data['timezone'] = 'America/Toronto'
    data['waivers'] = []
    if OUT_PATH.exists():
        try:
            data['waivers'] = json.loads(OUT_PATH.read_text()).get('waivers', [])
        except (ValueError, AttributeError):
            pass
    temp = OUT_PATH.with_suffix('.json.tmp')
    temp.write_text(json.dumps(data, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temp.replace(OUT_PATH)
    print(f"Wrote weekly and monthly stats: {len(data['skaters'])} weekly skaters, {len(data['monthly']['skaters'])} monthly skaters")


if __name__ == '__main__':
    main()
