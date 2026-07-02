#!/usr/bin/env python3
"""
Refresh data/matches.json with live FIFA World Cup 2026 knockout-stage data.

Run by .github/workflows/update-data.yml on a schedule (and manually via
workflow_dispatch). Designed to run on GitHub's hosted runners, which have
normal internet access.

Data sources:
  - Scores / fixtures / venues / goalscorers: ESPN's public (unofficial,
    no key required) scoreboard & summary JSON endpoints. This is the same
    endpoint many open-source scoreboards use. If ESPN changes shape, this
    script degrades gracefully (skips what it can't parse, keeps the rest
    of matches.json untouched).
  - Odds: the-odds-api.com, only if the ODDS_API_KEY secret/env var is set.
    Free tier: https://the-odds-api.com (500 requests/month). Without a
    key, odds are simply left as "not available" in the UI.

The script never wipes a round it couldn't find live data for -- it only
replaces a round's matches once it has found at least one real fixture for
that round, so the bracket skeleton (with TBD placeholders) stays intact
until real fixtures are known.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MATCHES_PATH = os.path.join(REPO_ROOT, "data", "matches.json")
COUNTRIES_PATH = os.path.join(REPO_ROOT, "data", "countries.json")

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary"

# Knockout stage window for the 2026 tournament (Round of 32 through the
# Final). Used only to ask ESPN for the right date range; round
# *classification* below prefers ESPN's own round text when present.
KNOCKOUT_START = "20260628"
KNOCKOUT_END = "20260721"

ROUND_ORDER = ["ro32", "ro16", "qf", "sf", "third", "final"]
ROUND_SLOT_COUNT = {"ro32": 16, "ro16": 8, "qf": 4, "sf": 2, "third": 1, "final": 1}
ROUND_NAMES = {
    "ro32": "Round of 32",
    "ro16": "Round of 16",
    "qf": "Quarterfinals",
    "sf": "Semifinals",
    "third": "Third Place Play-off",
    "final": "Final",
}

HEADERS = {"User-Agent": "world-cup-knockout-bracket/1.0 (github actions data fetch)"}


def http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def classify_round(event):
    """Best-effort round classification using ESPN's own text first."""
    text_bits = []
    try:
        comp = event["competitions"][0]
        for note in comp.get("notes", []) or []:
            if note.get("headline"):
                text_bits.append(note["headline"])
    except (KeyError, IndexError):
        pass
    for key in ("shortName", "name"):
        if event.get(key):
            text_bits.append(event[key])
    haystack = " | ".join(text_bits).lower()

    if "third place" in haystack or "3rd place" in haystack:
        return "third"
    if "quarterfinal" in haystack or "quarter-final" in haystack:
        return "qf"
    if "semifinal" in haystack or "semi-final" in haystack:
        return "sf"
    if "round of 32" in haystack:
        return "ro32"
    if "round of 16" in haystack:
        return "ro16"
    if "final" in haystack:
        return "final"
    return None  # unknown -- caller falls back to date-bucket heuristic


def date_bucket_fallback(event_date):
    """Rough fallback classification by date, only used when ESPN gives no
    round text at all. Boundaries are approximate."""
    try:
        d = datetime.strptime(event_date[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    buckets = [
        ("ro32", "2026-06-28", "2026-07-03"),
        ("ro16", "2026-07-04", "2026-07-07"),
        ("qf", "2026-07-08", "2026-07-11"),
        ("sf", "2026-07-12", "2026-07-15"),
        ("third", "2026-07-16", "2026-07-18"),
        ("final", "2026-07-19", "2026-07-21"),
    ]
    for rid, start, end in buckets:
        if start <= str(d) <= end:
            return rid
    return None


def load_country_lookup():
    try:
        data = load_json(COUNTRIES_PATH)
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def resolve_team_code(name, espn_abbrev, country_lookup):
    key = (name or "").strip().lower()
    info = country_lookup.get(key)
    if info:
        return info.get("fifa", (espn_abbrev or "TBD")[:3].upper()), info.get("iso2", "")
    fallback_code = (espn_abbrev or (name or "TBD")[:3]).upper()[:3]
    return fallback_code, ""


def espn_status(event):
    try:
        state = event["status"]["type"]["state"]
    except (KeyError, TypeError):
        return "scheduled"
    return {"pre": "scheduled", "in": "live", "post": "finished"}.get(state, "scheduled")


def fetch_scorers(event_id, home_id, away_id):
    home_scorers, away_scorers = [], []
    try:
        summary = http_get_json(f"{ESPN_SUMMARY}?event={event_id}")
        details = summary["header"]["competitions"][0].get("details", [])
        for play in details:
            play_type = (play.get("type", {}) or {}).get("text", "")
            if "goal" not in play_type.lower():
                continue
            athletes = play.get("athletesInvolved") or []
            scorer_name = athletes[0]["displayName"] if athletes else "Unknown"
            minute = (play.get("clock") or {}).get("displayValue", "")
            team_id = str((play.get("team") or {}).get("id", ""))
            entry = {"name": scorer_name, "minute": minute}
            if team_id == str(home_id):
                home_scorers.append(entry)
            elif team_id == str(away_id):
                away_scorers.append(entry)
    except Exception as exc:  # noqa: BLE001 - best effort, never fatal
        print(f"  (scorers unavailable for event {event_id}: {exc})")
    return home_scorers, away_scorers


def espn_event_to_match(event, country_lookup):
    comp = event["competitions"][0]
    competitors = comp["competitors"]
    home_c = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
    away_c = next((c for c in competitors if c.get("homeAway") == "away"), competitors[-1])

    status = espn_status(event)

    def team_block(comp_team):
        team = comp_team.get("team", {})
        name = team.get("displayName") or team.get("name") or "TBD"
        code, iso2 = resolve_team_code(name, team.get("abbreviation"), country_lookup)
        score = comp_team.get("score")
        try:
            score = int(float(score)) if score not in (None, "") else None
        except (ValueError, TypeError):
            score = None
        return {"name": name, "code": code, "iso2": iso2, "score": score, "scorers": []}

    home = team_block(home_c)
    away = team_block(away_c)

    if status in ("live", "finished"):
        home_scorers, away_scorers = fetch_scorers(
            event["id"], home_c.get("team", {}).get("id"), away_c.get("team", {}).get("id")
        )
        home["scorers"], away["scorers"] = home_scorers, away_scorers

    venue = None
    try:
        v = comp.get("venue", {})
        if v.get("fullName"):
            venue = {"name": v["fullName"], "city": (v.get("address") or {}).get("city", "")}
    except Exception:  # noqa: BLE001
        venue = None

    odds = None
    try:
        odds_list = comp.get("odds") or []
        if odds_list:
            o = odds_list[0]
            provider = (o.get("provider") or {}).get("name", "ESPN")
            home_ml = ((o.get("homeTeamOdds") or {}).get("moneyLine"))
            away_ml = ((o.get("awayTeamOdds") or {}).get("moneyLine"))
            draw_ml = (o.get("drawOdds") or {}).get("moneyLine")

            def american_to_decimal(ml):
                if ml is None:
                    return None
                ml = float(ml)
                return round(1 + ml / 100, 2) if ml > 0 else round(1 + 100 / abs(ml), 2)

            home_dec, away_dec, draw_dec = (
                american_to_decimal(home_ml),
                american_to_decimal(away_ml),
                american_to_decimal(draw_ml),
            )
            if home_dec or away_dec:
                odds = {"home": home_dec, "draw": draw_dec, "away": away_dec, "bookmaker": provider}
    except Exception:  # noqa: BLE001
        odds = None

    return {
        "status": status,
        "date": event.get("date"),
        "venue": venue,
        "home": home,
        "away": away,
        "odds": odds,
        "_espn_home_name": home["name"],
        "_espn_away_name": away["name"],
    }


def fetch_espn_events():
    url = f"{ESPN_SCOREBOARD}?dates={KNOCKOUT_START}-{KNOCKOUT_END}&limit=100"
    payload = http_get_json(url)
    return payload.get("events", [])


def apply_odds_api_fallback(rounds_by_id, api_key):
    """For matches still missing odds, try the-odds-api.com's soccer_fifa_world_cup market."""
    if not api_key:
        return
    url = (
        "https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds/?"
        + urllib.parse.urlencode(
            {"apiKey": api_key, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"}
        )
    )
    try:
        events = http_get_json(url)
    except Exception as exc:  # noqa: BLE001
        print(f"  (odds-api fallback unavailable: {exc})")
        return

    def norm(s):
        return (s or "").strip().lower()

    for rid, matches in rounds_by_id.items():
        for m in matches:
            if m.get("odds") or m["status"] == "finished":
                continue
            home_n, away_n = norm(m["home"]["name"]), norm(m["away"]["name"])
            match_event = next(
                (
                    e
                    for e in events
                    if {norm(e.get("home_team")), norm(e.get("away_team"))} == {home_n, away_n}
                ),
                None,
            )
            if not match_event or not match_event.get("bookmakers"):
                continue
            book = match_event["bookmakers"][0]
            h2h = next((mk for mk in book.get("markets", []) if mk["key"] == "h2h"), None)
            if not h2h:
                continue
            prices = {norm(o["name"]): o["price"] for o in h2h.get("outcomes", [])}
            m["odds"] = {
                "home": prices.get(home_n),
                "away": prices.get(away_n),
                "draw": prices.get("draw"),
                "bookmaker": book.get("title", "the-odds-api"),
            }


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Fetching FIFA World Cup 2026 knockout data...")

    try:
        current = load_json(MATCHES_PATH)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FATAL: could not read {MATCHES_PATH}: {exc}")
        sys.exit(1)

    country_lookup = load_country_lookup()

    try:
        events = fetch_espn_events()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not reach ESPN scoreboard endpoint: {exc}")
        print("Leaving data/matches.json unchanged.")
        sys.exit(1)

    print(f"ESPN returned {len(events)} event(s) in the knockout window.")

    grouped = {rid: [] for rid in ROUND_ORDER}
    unclassified = 0
    for event in events:
        rid = classify_round(event) or date_bucket_fallback(event.get("date"))
        if not rid:
            unclassified += 1
            continue
        try:
            match = espn_event_to_match(event, country_lookup)
        except Exception as exc:  # noqa: BLE001
            print(f"  (skipping event {event.get('id')}: {exc})")
            continue
        grouped[rid].append(match)

    if unclassified:
        print(f"Could not classify {unclassified} event(s) into a knockout round; skipped.")

    for rid in ROUND_ORDER:
        grouped[rid].sort(key=lambda m: m.get("date") or "")

    apply_odds_api_fallback(grouped, os.environ.get("ODDS_API_KEY"))

    rounds_by_id = {r["id"]: r for r in current["rounds"]}
    updated_any_round = False

    for rid in ROUND_ORDER:
        found = grouped.get(rid) or []
        if not found:
            continue  # keep existing skeleton/demo/previous data for this round
        updated_any_round = True
        skeleton_matches = rounds_by_id[rid]["matches"]
        new_matches = []
        for i, m in enumerate(found):
            slot = skeleton_matches[i] if i < len(skeleton_matches) else {}
            new_matches.append(
                {
                    "id": slot.get("id", f"{rid}-{i + 1}"),
                    "matchNumber": slot.get("matchNumber"),
                    "status": m["status"],
                    "date": m["date"],
                    "venue": m["venue"],
                    "home": {
                        "name": m["home"]["name"],
                        "code": m["home"]["code"],
                        "iso2": m["home"]["iso2"],
                        "score": m["home"]["score"],
                        "scorers": m["home"]["scorers"],
                    },
                    "away": {
                        "name": m["away"]["name"],
                        "code": m["away"]["code"],
                        "iso2": m["away"]["iso2"],
                        "score": m["away"]["score"],
                        "scorers": m["away"]["scorers"],
                    },
                    "odds": m["odds"],
                    "advancesTo": slot.get("advancesTo"),
                }
            )
        rounds_by_id[rid]["matches"] = new_matches
        print(f"  {ROUND_NAMES[rid]}: {len(new_matches)} match(es) updated from live data.")

    if not updated_any_round:
        print("No knockout fixtures found yet from ESPN (group stage likely still in progress).")
        print("Leaving data/matches.json unchanged (skeleton/demo data preserved).")
        sys.exit(0)

    current["rounds"] = [rounds_by_id[rid] for rid in ROUND_ORDER]
    current["lastUpdated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    current["source"] = "ESPN public scoreboard API" + (
        " + the-odds-api.com" if os.environ.get("ODDS_API_KEY") else ""
    )

    save_json(MATCHES_PATH, current)
    print(f"Wrote {MATCHES_PATH}")


if __name__ == "__main__":
    main()
