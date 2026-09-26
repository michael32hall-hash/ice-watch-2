# Ice Watch

A fantasy hockey tracker that updates itself nightly via GitHub Actions.

## What's in here
- `index.html` — the site. Reads `data.json` on load.
- `data.json` — the data file the site displays. Starts empty; the nightly
  job overwrites it.
- `scripts/fetch_nhl_stats.py` — pulls the last 7 days of NHL box scores and
  aggregates skater/goalie stats.
- `scripts/fetch_espn_ownership.py` — pulls trending-up ownership % from
  ESPN's public fantasy hockey data.
- `.github/workflows/update.yml` — runs both scripts every night and commits
  the result.

## One-time setup

1. **Create a GitHub repo** and push everything in this folder to it (root
   of the repo, not a subfolder).

2. **Allow Actions to push commits.** Go to your repo's
   `Settings → Actions → General → Workflow permissions` and select
   **"Read and write permissions."** Without this, the nightly job can fetch
   data but can't commit the updated `data.json`.

3. **Turn on GitHub Pages.** Go to `Settings → Pages`, set
   **Source: Deploy from a branch**, branch `main`, folder `/ (root)`. GitHub
   will give you a URL like `https://yourname.github.io/icewatch/` — that's
   the link to share with your league.

4. **Run it once manually** to check it works: go to the **Actions** tab,
   pick "Nightly stats update," and click **Run workflow**. Check the log —
   if `data.json` gets a real commit with player names in it, you're set.

## About the data sources

- **NHL stats** come from `api-web.nhle.com`, the NHL's own public API. It's
  not officially documented, so field names in `fetch_nhl_stats.py` are
  best-effort. A few categories (shorthanded goals/assists, game-winning
  goal, faceoff win *counts*) aren't reliably exposed at the per-player level
  in this endpoint and are left at 0 — the comments in the script explain why.
  If something errors out or looks wrong once real games are being played,
  bring the Actions log or a sample API response back to this chat and I can
  help fix the field mapping — I can't test the live API myself since I don't
  have outbound network access here.

- **ESPN ownership** comes from a public, read-only endpoint (no login
  needed) that ESPN's own "trending" widgets use internally.

- **Yahoo ownership** isn't wired up yet. Yahoo's Fantasy Sports API needs a
  registered developer app and a full OAuth login flow, which didn't fit in
  this first pass — the Yahoo columns just show 0 for now. Let me know if you
  want to add it next.

## Scoring settings

These live in the browser (localStorage), not in `data.json` — each visitor
sets their own league's point values in the "Scoring settings" panel on the
page, and it's remembered on their device. Nothing to configure server-side.

## Changing the schedule

The cron line in `.github/workflows/update.yml` (`0 9 * * *`) runs at 9:00
UTC daily. Adjust the time as you like — crontab.guru is handy for building
the expression.
