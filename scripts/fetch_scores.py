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
import re
import sys
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
STATUS_RANK = {"finished": 2, "live": 1, "scheduled": 0}

# Consecutive rounds, current -> next, used for name-based advancesTo linking.
# (sf -> final only; the third-place match is an unconnected side branch.)
ADVANCE_CHAIN = [("ro32", "ro16"), ("ro16", "qf"), ("qf", "sf"), ("sf", "final")]

# Fixed feeder structure for the *upper* bracket (Round of 16 onward). This is
# the one part of the tree that is safe to hardcode: it was read directly from
# ESPN's own placeholder labels, which for these rounds are positional and
# consistent -- e.g. m97 is labelled "Round of 16 1 Winner vs Round of 16 2
# Winner", and the 1st/2nd Round-of-16 matches by number are m89/m90. So:
#   m97<-(m89,m90)  m98<-(m93,m94)  m99<-(m91,m92)  m100<-(m95,m96)
#   m101<-(m97,m98) m102<-(m99,m100)                m104<-(m101,m102)
# The Round-of-32 -> Round-of-16 mapping is deliberately NOT hardcoded here:
# ESPN's "Round of 32 N Winner" ordinals are NOT positional (e.g. m93's open
# slot is "Round of 32 11 Winner" even though the 11th RO32 match, m83, is
# already filled into m93). Guessing it produced wrong/missing lines, so that
# transition is resolved by real team names only (see link_advances_to).
UPPER_BRACKET_ADVANCE = {
    "m89": "m97", "m90": "m97", "m93": "m98", "m94": "m98",
    "m91": "m99", "m92": "m99", "m95": "m100", "m96": "m100",
    "m97": "m101", "m98": "m101", "m99": "m102", "m100": "m102",
    "m101": "m104", "m102": "m104",
}

# A team name that is really a TBD placeholder, never matched by name.
PLACEHOLDER_NAME_RE = re.compile(r"\b(winner|loser|tbd)\b", re.IGNORECASE)


def build_canonical_skeleton():
    """Fixed match id / matchNumber per bracket slot, independent of
    whatever currently happens to be in data/matches.json. Positional
    mapping used to read this from the live file, which meant a round that
    briefly had fewer real fixtures than its slot count (e.g. only 2 of 4
    Quarterfinals found on an early run) permanently lost the proper ids for
    the missing slots on every run after. Recomputing this fresh each run
    makes that class of drift impossible.

    Note this only fixes *ids*, not bracket connections -- see
    link_advances_to() for how those are derived."""
    ro32_ids = [f"m{n}" for n in range(73, 89)]
    ro16_ids = [f"m{n}" for n in range(89, 97)]
    qf_ids = [f"m{n}" for n in range(97, 101)]
    sf_ids = ["m101", "m102"]

    def slots(ids):
        return [{"id": mid, "matchNumber": int(mid[1:])} for mid in ids]

    return {
        "ro32": slots(ro32_ids),
        "ro16": slots(ro16_ids),
        "qf": slots(qf_ids),
        "sf": slots(sf_ids),
        "third": [{"id": "m103", "matchNumber": 103}],
        "final": [{"id": "m104", "matchNumber": 104}],
    }


CANONICAL_SKELETON = build_canonical_skeleton()

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
    """Best-effort round classification using ESPN's own round-label notes.

    Deliberately ignores shortName/name: for a fixture whose teams aren't
    decided yet, those fields are just "<team A> vs <team B>", and the TBD
    placeholder team name describes the *previous* round's winner (e.g.
    "Round of 32 11 Winner" is genuinely a Round-of-16 fixture). Scanning
    that text for round keywords used to misclassify the match one round
    too early -- this is why extra/misplaced cards were showing up.
    """
    try:
        comp = event["competitions"][0]
        headlines = [n["headline"] for n in (comp.get("notes") or []) if n.get("headline")]
    except (KeyError, IndexError, TypeError):
        headlines = []
    haystack = " | ".join(headlines).lower()
    if not haystack:
        return None  # no round label at all -- caller falls back to date-bucket heuristic

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
    return None


def date_bucket_fallback(event_date):
    """Fallback classification by kickoff time, only used when ESPN's event
    carries no round-label notes. Boundaries are full timestamps (not just
    calendar dates) with a ~9-hour grace past midnight UTC, since a North
    American evening kickoff can land after midnight UTC and would
    otherwise roll into the next round's date bucket."""
    try:
        d = datetime.strptime(event_date[:16], "%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return None
    iso = d.strftime("%Y-%m-%dT%H:%M")
    buckets = [
        ("ro32", "2026-06-28T00:00", "2026-07-04T09:00"),
        ("ro16", "2026-07-04T09:00", "2026-07-08T09:00"),
        ("qf", "2026-07-08T09:00", "2026-07-12T09:00"),
        ("sf", "2026-07-12T09:00", "2026-07-16T09:00"),
        ("third", "2026-07-16T09:00", "2026-07-19T09:00"),
        ("final", "2026-07-19T09:00", "2026-07-21T00:00"),
    ]
    for rid, start, end in buckets:
        if start <= iso < end:
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
    """Best-effort goalscorer extraction. ESPN's summary endpoint is
    undocumented and has shown a couple of different shapes for where
    scoring plays live, so this tries several candidate paths and logs what
    it found (or didn't) rather than failing silently -- if goals are still
    missing after a run, the Action log for this step will say why."""
    home_scorers, away_scorers = [], []
    try:
        summary = http_get_json(f"{ESPN_SUMMARY}?event={event_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"  (event {event_id}: summary fetch failed: {exc})")
        return home_scorers, away_scorers

    candidates, source = [], None
    try:
        details = summary["header"]["competitions"][0].get("details") or []
        if details:
            candidates, source = details, "header.competitions[0].details"
    except (KeyError, IndexError, TypeError):
        pass

    if not candidates:
        for key in ("keyEvents", "plays"):
            items = summary.get(key) or []
            if items:
                candidates, source = items, key
                break

    goal_count = 0
    for play in candidates:
        play_type_text = ((play.get("type") or {}).get("text")) or play.get("text") or ""
        is_scoring = play.get("scoringPlay") is True or "goal" in play_type_text.lower()
        if not is_scoring:
            continue
        goal_count += 1

        athletes = (
            play.get("athletesInvolved")
            or play.get("athletes")
            or [p.get("athlete") for p in (play.get("participants") or []) if p.get("athlete")]
            or []
        )
        first = athletes[0] if athletes else None
        scorer_name = first.get("displayName", "Unknown") if isinstance(first, dict) else "Unknown"
        minute = (play.get("clock") or {}).get("displayValue", "")
        team_id = str((play.get("team") or {}).get("id", ""))

        entry = {"name": scorer_name, "minute": minute}
        if team_id == str(home_id):
            home_scorers.append(entry)
        elif team_id == str(away_id):
            away_scorers.append(entry)
        else:
            print(f"  (event {event_id}: goal team id {team_id!r} matched neither home nor away)")

    if not candidates:
        print(f"  (event {event_id}: no scoring-play data in summary response; top-level keys: {list(summary.keys())})")
    elif goal_count == 0:
        print(f"  (event {event_id}: {len(candidates)} item(s) in '{source}' but none looked like goals)")

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
    }


def fetch_espn_events():
    url = f"{ESPN_SCOREBOARD}?dates={KNOCKOUT_START}-{KNOCKOUT_END}&limit=100"
    payload = http_get_json(url)
    return payload.get("events", [])


def dedupe_matches(matches):
    """Collapse duplicate entries for the same pairing (can happen if ESPN
    lists a fixture more than once), keeping the most complete version:
    finished > live > scheduled, then whichever has venue info."""
    best_by_pair = {}
    order = []
    for m in matches:
        key = frozenset({m["home"]["name"].strip().lower(), m["away"]["name"].strip().lower()})
        existing = best_by_pair.get(key)
        if existing is None:
            best_by_pair[key] = m
            order.append(key)
            continue
        existing_rank = (STATUS_RANK.get(existing["status"], 0), bool(existing.get("venue")))
        new_rank = (STATUS_RANK.get(m["status"], 0), bool(m.get("venue")))
        if new_rank > existing_rank:
            best_by_pair[key] = m
    return [best_by_pair[k] for k in order]


def cap_to_slots(rid, matches):
    """Safety net: a knockout round can only ever have this many matches.
    If classification still overshoots for some reason, keep the earliest
    N by kickoff time and log it loudly rather than showing extra cards."""
    limit = ROUND_SLOT_COUNT[rid]
    if len(matches) <= limit:
        return matches
    print(
        f"  WARNING: {ROUND_NAMES[rid]} had {len(matches)} candidate fixture(s) after "
        f"dedup, more than its {limit} slots. Keeping the {limit} earliest by kickoff "
        f"and dropping the rest."
    )
    return matches[:limit]


def link_advances_to(rounds_by_id):
    """Sets each match's advancesTo to the next-round match its winner plays in.

    Two sources, in order of trust:

    1. Real team names. If a team in a current-round match also appears in a
       next-round match, that match is where its winner goes -- unambiguous and
       always correct once either feeder is decided. This is the ONLY source for
       Round of 32 -> Round of 16, because ESPN's "Round of 32 N Winner"
       ordinals there are not positional and guessing them drew wrong lines.

    2. Fixed upper-bracket structure (UPPER_BRACKET_ADVANCE) for Round of 16
       onward, whose feeder pairing is stable and was verified against ESPN's
       own positional labels. This keeps the upper bracket fully connected even
       before those teams are known.

    Anything unresolved is left None -- no connector is drawn rather than a
    wrong one. Round-of-32 matches whose winners haven't been slotted into the
    Round of 16 yet simply gain their line once those results come in.
    """
    def is_real(name):
        return bool(name) and not PLACEHOLDER_NAME_RE.search(name)

    # Pass 1: link by real team names across every consecutive round.
    for cur_id, nxt_id in ADVANCE_CHAIN:
        cur_matches = rounds_by_id[cur_id]["matches"]
        nxt_matches = rounds_by_id[nxt_id]["matches"]
        for m in cur_matches:
            m["advancesTo"] = None
            names = {n.strip().lower() for n in (m["home"]["name"], m["away"]["name"]) if is_real(n)}
            if not names:
                continue
            for nm in nxt_matches:
                nxt_names = {n.strip().lower() for n in (nm["home"]["name"], nm["away"]["name"]) if is_real(n)}
                if names & nxt_names:
                    m["advancesTo"] = nm["id"]
                    break

    # Pass 2: fill still-unresolved links in the upper bracket from the fixed
    # verified structure (does not touch Round of 32 -> Round of 16).
    ids_present = {m["id"] for r in rounds_by_id.values() for m in r["matches"]}
    for r in rounds_by_id.values():
        for m in r["matches"]:
            if not m.get("advancesTo") and m["id"] in UPPER_BRACKET_ADVANCE:
                target = UPPER_BRACKET_ADVANCE[m["id"]]
                if target in ids_present:
                    m["advancesTo"] = target


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
        grouped[rid] = dedupe_matches(grouped[rid])
        grouped[rid] = cap_to_slots(rid, grouped[rid])

    apply_odds_api_fallback(grouped, os.environ.get("ODDS_API_KEY"))

    rounds_by_id = {r["id"]: r for r in current["rounds"]}
    updated_any_round = False

    for rid in ROUND_ORDER:
        found = grouped.get(rid) or []
        if not found:
            continue  # keep existing skeleton/demo/previous data for this round
        updated_any_round = True
        skeleton_matches = CANONICAL_SKELETON[rid]
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
                    "advancesTo": None,
                }
            )
        rounds_by_id[rid]["matches"] = new_matches
        print(f"  {ROUND_NAMES[rid]}: {len(new_matches)} match(es) updated from live data.")

    if not updated_any_round:
        print("No knockout fixtures found yet from ESPN (group stage likely still in progress).")
        print("Leaving data/matches.json unchanged (skeleton/demo data preserved).")
        sys.exit(0)

    link_advances_to(rounds_by_id)

    current["rounds"] = [rounds_by_id[rid] for rid in ROUND_ORDER]
    current["lastUpdated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    current["source"] = "ESPN public scoreboard API" + (
        " + the-odds-api.com" if os.environ.get("ODDS_API_KEY") else ""
    )

    save_json(MATCHES_PATH, current)
    print(f"Wrote {MATCHES_PATH}")


if __name__ == "__main__":
    main()
