# FIFA World Cup 2026 — Knockout Stage Bracket

A single-page, framework-free interactive bracket for the World Cup knockout
stage: Round of 32 → Round of 16 → Quarterfinals → Semifinals → Third Place
Play-off → Final. Click any fixture for venue, kickoff time, odds to win, and
(once played) the final score and goalscorers. Country flags render as emoji
next to each team's FIFA code. It's plain HTML/CSS/JS, free to host on
GitHub Pages, and keeps itself up to date via a scheduled GitHub Actions job.

## How it's put together

```
index.html              the page shell
assets/style.css         styling (light/dark mode aware)
assets/app.js             renders the bracket + modal from data/matches.json, polls every 5 min
data/matches.json         the single source of truth the page reads
data/countries.json       team name -> FIFA code / ISO2 lookup (for flags)
scripts/fetch_scores.py   pulls live data and rewrites data/matches.json
.github/workflows/
  update-data.yml          runs fetch_scores.py every 15 min, commits changes
```

There's no build step and no server — GitHub Actions edits a JSON file in the
repo, GitHub Pages serves the repo, and the browser polls the JSON file.
Pages is configured here to **deploy from the branch** (Settings → Pages →
Source → "Deploy from a branch"), so GitHub's own `pages build and
deployment` job republishes the site automatically every time
`update-data.yml` commits fresh data — no separate deploy workflow needed.

## One-time setup

1. **Merge this branch to your default branch** (e.g. `main`). Scheduled
   (`cron`) workflows only fire on the default branch, so `update-data.yml`
   stays dormant until then — you can still run it manually from the Actions
   tab in the meantime via "Run workflow".
2. **Enable Pages**: repo Settings → Pages → Source → **Deploy from a
   branch** → pick `main` / `/ (root)`. GitHub then rebuilds and republishes
   the site on every push automatically, including the commits
   `update-data.yml` makes.
3. **Enable Actions write access**: Settings → Actions → General →
   Workflow permissions → **Read and write permissions** (needed so
   `update-data.yml` can commit the refreshed `data/matches.json` back to
   the repo).
4. *(Optional, for odds)* Get a free API key at
   [the-odds-api.com](https://the-odds-api.com/) (500 requests/month free)
   and add it as a repository secret named `ODDS_API_KEY`
   (Settings → Secrets and variables → Actions). Without it, the site still
   works — the odds panel just shows "not available" until a match is
   played.

Once merged, the site is live at `https://<your-username>.github.io/<repo>/`.

## How the data updates

`scripts/fetch_scores.py` runs every 15 minutes on GitHub's runners (which,
unlike this dev sandbox, have normal internet access) and:

1. Pulls the current FIFA World Cup 2026 scoreboard from ESPN's public
   (unofficial, no API key needed) soccer JSON endpoint, restricted to the
   knockout-stage date window.
2. Classifies each fixture into a round (Round of 32 … Final) using ESPN's
   own round labels, with a date-based fallback.
3. For live/finished matches, fetches the play-by-play summary and pulls out
   goalscorers and minutes.
4. If `ODDS_API_KEY` is set and ESPN doesn't already supply odds for a
   fixture, looks up moneyline/h2h odds from the-odds-api.com and converts
   them to decimal odds + implied win probability.
5. Writes the merged result back to `data/matches.json` and commits it —
   which GitHub's branch-deploy Pages job then automatically republishes.

**A round is only overwritten once real fixtures are found for it.** Until
then it keeps showing the placeholder bracket (`TBD vs TBD`) so the shape of
the draw is always visible, even before teams are confirmed.

**Checking it's actually running:** repo → Actions tab → "Update knockout
stage data" → open any run and expand the "Fetch live scores..." step. It
prints exactly what it found, e.g. `ESPN returned 32 event(s)...` and a
per-round match count. You can also click "Run workflow" there to trigger an
immediate refresh instead of waiting for the next scheduled tick.

## The "SAMPLE DATA" cards

`data/matches.json` ships with two example fixtures (marked `isDemo: true`)
so the interface has something real to show — a finished match with
goalscorers, and an upcoming one with odds — before the first live fetch
runs. They're visually flagged with a "SAMPLE DATA" ribbon and a banner in
the popup, and get replaced automatically the first time `fetch_scores.py`
finds real Round of 32 fixtures.

## Running / previewing locally

It's static files — any local web server works (a plain `file://` open
won't, because `fetch()` for `data/matches.json` needs http):

```bash
python3 -m http.server 8000
# open http://localhost:8000
```

To test the data fetch script locally (it only uses the Python standard
library, so there's nothing to install):

```bash
python3 scripts/fetch_scores.py
```

## Swapping data sources

ESPN's endpoint is unofficial and undocumented — if it ever changes shape or
gets blocked, edit `scripts/fetch_scores.py`'s `fetch_espn_events` /
`espn_event_to_match` functions to point at a different provider (e.g.
football-data.org, api-football). The rest of the pipeline (schema, bracket
rendering, modal, odds merge) doesn't need to change as long as the script
still writes the same `data/matches.json` shape.
