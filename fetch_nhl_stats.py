def fetch_boxscore(game_id):

    game_id = str(game_id)
    last_error = None

    for attempt in range(5):

        try:

            print(
                f"Fetching game {game_id} "
                f"(attempt {attempt + 1}/5)"
            )

            box = get_json(
                f"{NHL_API}/gamecenter/"
                f"{game_id}/boxscore"
            )

            pbp = get_json(
                f"{NHL_API}/gamecenter/"
                f"{game_id}/play-by-play"
            )

            landing = get_json(
                f"{NHL_API}/gamecenter/"
                f"{game_id}/landing"
            )

            result = supplement_skater_stats(
                box,
                pbp,
                landing,
            )

            print(
                f"Game {game_id} fetched successfully"
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
