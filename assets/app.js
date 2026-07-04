(() => {
  "use strict";

  const REFRESH_MS = 2 * 60 * 1000; // poll every 2 minutes for "live" updates
  const MAIN_ROUND_IDS = ["ro32", "ro16", "qf", "sf", "final"];
  const SIDE_ROUND_IDS = ["third"];

  // Where to read match data from. On the published GitHub Pages site we read
  // straight from the repo's default branch via raw.githubusercontent.com,
  // NOT from the Pages-served copy. Reason: the data-refresh Action commits new
  // scores every few minutes, and each commit needs a Pages redeploy to become
  // visible — but those redeploys transiently fail ("Deployment failed, try
  // again later"), which would freeze scores on the live site for hours. Reading
  // the raw file decouples live data from Pages deploy health entirely: the
  // static shell only has to deploy once, and scores then update on their own.
  function dataUrl() {
    try {
      const host = window.location.hostname;
      if (host.endsWith("github.io")) {
        const user = host.split(".")[0];
        const repo = window.location.pathname.split("/").filter(Boolean)[0];
        if (user && repo) {
          return `https://raw.githubusercontent.com/${user}/${repo}/main/data/matches.json`;
        }
      }
    } catch {
      /* fall through to the local relative path (local dev / other hosts) */
    }
    return "data/matches.json";
  }

  const FLAG_OVERRIDES = {
    "GB-ENG": "\u{1F3F4}\u{E0067}\u{E0062}\u{E0065}\u{E006E}\u{E0067}\u{E007F}",
    "GB-SCT": "\u{1F3F4}\u{E0067}\u{E0062}\u{E0073}\u{E0063}\u{E0074}\u{E007F}",
    "GB-WLS": "\u{1F3F4}\u{E0067}\u{E0062}\u{E0077}\u{E006C}\u{E0073}\u{E007F}",
    "GB-NIR": "\u{1F1EC}\u{1F1E7}"
  };

  function flagEmoji(iso2) {
    if (!iso2) return "\u{1F3F3}\u{FE0F}"; // white flag placeholder
    if (FLAG_OVERRIDES[iso2]) return FLAG_OVERRIDES[iso2];
    if (iso2.length !== 2) return "\u{1F3F3}\u{FE0F}";
    const codePoints = [...iso2.toUpperCase()].map((c) => 127397 + c.charCodeAt(0));
    try {
      return String.fromCodePoint(...codePoints);
    } catch {
      return "\u{1F3F3}\u{FE0F}";
    }
  }

  function fmtDateTime(iso) {
    if (!iso) return "Date/time TBD";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "Date/time TBD";
    return d.toLocaleString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  }

  function fmtUpdated(iso) {
    if (!iso) return "not yet — showing sample data";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "unknown";
    return d.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    });
  }

  function fmtShortDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  // A match was decided on penalties if both teams carry a penalty tally.
  function hasShootout(match) {
    return (
      match.home && match.away &&
      match.home.penalties !== null && match.home.penalties !== undefined &&
      match.away.penalties !== null && match.away.penalties !== undefined
    );
  }

  // Which side won: "home" | "away" | null. Falls back to the penalty tally
  // when regulation/extra-time scores are level (i.e. a shootout).
  function winnerSide(match) {
    const h = match.home, a = match.away;
    if (h.score == null || a.score == null) return null;
    if (h.score > a.score) return "home";
    if (a.score > h.score) return "away";
    if (hasShootout(match)) {
      if (h.penalties > a.penalties) return "home";
      if (a.penalties > h.penalties) return "away";
    }
    return null;
  }

  function statusLabel(match) {
    if (match.status === "live") return "LIVE";
    if (match.status === "finished") {
      const d = fmtShortDate(match.date);
      return "FT" + (hasShootout(match) ? " (pens)" : "") + (d ? " · " + d : "");
    }
    return fmtDateTime(match.date);
  }

  function teamRowHTML(team, isFinished, isWinner, penScore) {
    const tbd = !team || !team.name || team.name === "TBD";
    const flag = flagEmoji(team && team.iso2);
    const code = (team && team.code) || "—";
    const name = (team && team.name) || "TBD";
    const hasScore = isFinished && team && team.score !== null && team.score !== undefined;
    const pens =
      hasScore && penScore !== null && penScore !== undefined
        ? ` <span class="pens">(${penScore})</span>`
        : "";
    return `<div class="team-row${tbd ? " tbd" : ""}${isWinner ? " winner" : ""}">
      <span class="flag">${flag}</span>
      <span class="code">${escapeHTML(code)}</span>
      <span class="name">${escapeHTML(name)}</span>
      <span class="score">${hasScore ? team.score : ""}${pens}</span>
    </div>`;
  }

  function escapeHTML(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function matchCardHTML(match) {
    const isFinished = match.status === "finished";
    const statusClass = match.status === "live" ? "live" : match.status === "finished" ? "finished" : "";
    const dot = match.status === "live" ? '<span class="live-dot"></span>' : "";
    const win = isFinished ? winnerSide(match) : null;
    const pens = hasShootout(match);
    return `<div class="match-card${match.isDemo ? " demo" : ""}" data-match-id="${match.id}" tabindex="0" role="button" aria-haspopup="dialog">
      <div class="match-status-bar">
        <span class="status-badge ${statusClass}">${dot}${statusLabel(match)}</span>
        <span>#${match.matchNumber}</span>
      </div>
      ${teamRowHTML(match.home, isFinished, win === "home", pens ? match.home.penalties : null)}
      ${teamRowHTML(match.away, isFinished, win === "away", pens ? match.away.penalties : null)}
    </div>`;
  }

  // Reorder each round's matches so the bracket tree lays out cleanly: the two
  // matches that feed the same next-round match are placed adjacently, in the
  // order their shared parent appears in the next round. Without this, cards sit
  // in match-number order — but the real bracket pairing isn't sequential (e.g.
  // m73 and m76 both feed m89), so the connector lines cross and look wrong.
  // We derive the layout purely from advancesTo, working from the final backward
  // so each round is ordered to match its already-ordered successor. With even
  // pairing and CSS space-around, every parent then sits centered between its
  // two children and the connectors form clean brackets.
  function orderRoundsByBracket(data) {
    const roundById = {};
    data.rounds.forEach((r) => (roundById[r.id] = r));
    for (let i = MAIN_ROUND_IDS.length - 2; i >= 0; i--) {
      const cur = roundById[MAIN_ROUND_IDS[i]];
      const nxt = roundById[MAIN_ROUND_IDS[i + 1]];
      if (!cur || !nxt || cur.matches.length === 0 || nxt.matches.length === 0) continue;
      const pos = {};
      nxt.matches.forEach((m, idx) => (pos[m.id] = idx));
      const BIG = Number.MAX_SAFE_INTEGER;
      // Composite key: primary = parent's position in the next round, secondary
      // = original index (keeps a parent's two feeders in stable order, and
      // leaves unlinked matches at the end in their existing order).
      cur.matches = cur.matches
        .map((m, idx) => {
          const p = m.advancesTo != null && pos[m.advancesTo] != null ? pos[m.advancesTo] : BIG;
          return { m, key: p * 1000 + idx };
        })
        .sort((a, b) => a.key - b.key)
        .map((x) => x.m);
    }
  }

  function renderBracket(data) {
    const byId = {};
    data.rounds.forEach((r) => (byId[r.id] = r));

    const bracketEl = document.getElementById("bracket");
    bracketEl.innerHTML = "";
    MAIN_ROUND_IDS.forEach((rid) => {
      const round = byId[rid];
      if (!round) return;
      const col = document.createElement("div");
      col.className = "round-col";
      col.innerHTML = `<div class="round-title">${escapeHTML(round.name)}</div>
        <div class="round-matches">${round.matches.map(matchCardHTML).join("")}</div>`;
      bracketEl.appendChild(col);
    });

    const sideEl = document.getElementById("side-branch");
    sideEl.innerHTML = "";
    SIDE_ROUND_IDS.forEach((rid) => {
      const round = byId[rid];
      if (!round) return;
      const wrap = document.createElement("div");
      wrap.innerHTML = `<h2>${escapeHTML(round.name)}</h2>${round.matches.map(matchCardHTML).join("")}`;
      sideEl.appendChild(wrap);
    });

    // wire clicks
    bracketEl.querySelectorAll(".match-card").forEach((card) => {
      card.addEventListener("click", () => openModal(card.dataset.matchId, data));
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openModal(card.dataset.matchId, data);
        }
      });
    });
    sideEl.querySelectorAll(".match-card").forEach((card) => {
      card.addEventListener("click", () => openModal(card.dataset.matchId, data));
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openModal(card.dataset.matchId, data);
        }
      });
    });

    requestAnimationFrame(() => drawConnectors(data));

    document.getElementById("last-updated").textContent = fmtUpdated(data.lastUpdated);
    document.getElementById("empty-note").hidden = !!data.lastUpdated;

    const anyLive = data.rounds.some((r) => r.matches.some((m) => m.status === "live"));
    document.getElementById("live-indicator").hidden = !anyLive;

    // best-effort repo link for the footer
    try {
      const host = window.location.hostname;
      if (host.endsWith("github.io")) {
        const user = host.split(".")[0];
        const path = window.location.pathname.split("/").filter(Boolean);
        const repo = path[0];
        if (user && repo) {
          document.getElementById("repo-link").href = `https://github.com/${user}/${repo}`;
        }
      }
    } catch {
      /* non-critical */
    }
  }

  const SVG_NS = "http://www.w3.org/2000/svg";

  function drawConnectors(data) {
    const bracket = document.getElementById("bracket");
    let svg = document.getElementById("connectors-svg");
    if (!svg) {
      svg = document.createElementNS(SVG_NS, "svg");
      svg.id = "connectors-svg";
      svg.setAttribute("aria-hidden", "true");
      bracket.prepend(svg);
    }
    svg.innerHTML = "";

    const cardsById = {};
    bracket.querySelectorAll(".match-card").forEach((c) => {
      cardsById[c.dataset.matchId] = c;
    });
    const bracketRect = bracket.getBoundingClientRect();

    data.rounds.forEach((round) => {
      if (!MAIN_ROUND_IDS.includes(round.id)) return;
      round.matches.forEach((m) => {
        if (!m.advancesTo) return;
        const srcCard = cardsById[m.id];
        const tgtCard = cardsById[m.advancesTo];
        if (!srcCard || !tgtCard) return;
        const srcRect = srcCard.getBoundingClientRect();
        const tgtRect = tgtCard.getBoundingClientRect();
        const x1 = srcRect.right - bracketRect.left;
        const y1 = srcRect.top + srcRect.height / 2 - bracketRect.top;
        const x2 = tgtRect.left - bracketRect.left;
        const y2 = tgtRect.top + tgtRect.height / 2 - bracketRect.top;
        const xMid = x1 + (x2 - x1) / 2;
        const path = document.createElementNS(SVG_NS, "path");
        path.setAttribute("d", `M${x1},${y1} H${xMid} V${y2} H${x2}`);
        path.setAttribute("class", "connector-line");
        svg.appendChild(path);
      });
    });
  }

  function findMatch(data, id) {
    for (const round of data.rounds) {
      const m = round.matches.find((mm) => mm.id === id);
      if (m) return { match: m, round };
    }
    return null;
  }

  function oddsRowsHTML(odds) {
    if (!odds || (odds.home == null && odds.away == null)) {
      return `<p class="no-odds">Odds not available yet.</p>`;
    }
    const implied = (o) => (o ? `${(100 / o).toFixed(1)}%` : "—");
    return `<table class="odds-table">
      <thead><tr><th></th><th>Home</th>${odds.draw != null ? "<th>Draw</th>" : ""}<th>Away</th></tr></thead>
      <tbody>
        <tr><td>Decimal odds</td>
          <td class="odds-val">${odds.home != null ? odds.home.toFixed(2) : "—"}</td>
          ${odds.draw != null ? `<td class="odds-val">${odds.draw.toFixed(2)}</td>` : ""}
          <td class="odds-val">${odds.away != null ? odds.away.toFixed(2) : "—"}</td>
        </tr>
        <tr><td>Implied chance</td>
          <td class="odds-val">${implied(odds.home)}</td>
          ${odds.draw != null ? `<td class="odds-val">${implied(odds.draw)}</td>` : ""}
          <td class="odds-val">${implied(odds.away)}</td>
        </tr>
      </tbody>
    </table>
    <p class="odds-source">Source: ${escapeHTML(odds.bookmaker || "unknown")}. For information only — not betting advice.</p>`;
  }

  function scorersHTML(match) {
    if (match.status !== "finished") return "";
    const homeScorers = (match.home.scorers || [])
      .map((s) => `<li>${escapeHTML(s.name)} <span style="color:var(--text-muted)">${escapeHTML(s.minute || "")}</span></li>`)
      .join("") || "<li>No goals</li>";
    const awayScorers = (match.away.scorers || [])
      .map((s) => `<li>${escapeHTML(s.name)} <span style="color:var(--text-muted)">${escapeHTML(s.minute || "")}</span></li>`)
      .join("") || "<li>No goals</li>";
    return `<div class="section-title">Goalscorers</div>
      <div class="scorers-cols">
        <div><div class="col-head">${escapeHTML(match.home.code || match.home.name)}</div><ul>${homeScorers}</ul></div>
        <div><div class="col-head">${escapeHTML(match.away.code || match.away.name)}</div><ul>${awayScorers}</ul></div>
      </div>`;
  }

  function openModal(matchId, data) {
    const found = findMatch(data, matchId);
    if (!found) return;
    const { match, round } = found;
    const isFinished = match.status === "finished";

    document.getElementById("modal-title").textContent = `${match.home.name} vs ${match.away.name}`;
    document.getElementById("modal-sub").textContent = `${round.name} · Match #${match.matchNumber}`;

    const body = document.getElementById("modal-body");
    const win = isFinished ? winnerSide(match) : null;
    const shootout = isFinished && hasShootout(match);
    const penLine = shootout
      ? `<div class="modal-pens">${match.home.penalties}–${match.away.penalties} on penalties</div>`
      : "";
    const scoreOrVs = isFinished
      ? `<div class="modal-score-wrap"><div class="modal-score">${match.home.score ?? "-"} : ${match.away.score ?? "-"}</div>${penLine}</div>`
      : `<div class="modal-vs">VS</div>`;

    body.innerHTML = `
      ${match.isDemo ? `<div class="modal-demo-banner">This is sample data used to preview the interface. Real fixtures, results and odds populate automatically once the data-fetch workflow runs.</div>` : ""}
      <div class="modal-teams">
        <div class="modal-team${win === "home" ? " winner" : ""}">
          <span class="flag">${flagEmoji(match.home.iso2)}</span>
          <div class="name">${escapeHTML(match.home.name)}</div>
          <div class="code">${escapeHTML(match.home.code || "")}</div>
        </div>
        ${scoreOrVs}
        <div class="modal-team${win === "away" ? " winner" : ""}">
          <span class="flag">${flagEmoji(match.away.iso2)}</span>
          <div class="name">${escapeHTML(match.away.name)}</div>
          <div class="code">${escapeHTML(match.away.code || "")}</div>
        </div>
      </div>
      <dl class="info-grid">
        <dt>Status</dt><dd>${match.status === "live" ? "Live now" : match.status === "finished" ? (shootout ? "Finished (a.p.)" : "Finished") : "Scheduled"}</dd>
        <dt>${isFinished ? "Played" : "Kickoff"}</dt><dd>${fmtDateTime(match.date)}</dd>
        <dt>Venue</dt><dd>${match.venue ? `${escapeHTML(match.venue.name)}${match.venue.city ? ", " + escapeHTML(match.venue.city) : ""}` : "TBD"}</dd>
      </dl>
      ${scorersHTML(match)}
      ${!isFinished ? `<div class="section-title">Odds to win</div>${oddsRowsHTML(match.odds)}` : ""}
    `;

    document.getElementById("modal-overlay").hidden = false;
    document.getElementById("modal-close").focus();
  }

  function closeModal() {
    document.getElementById("modal-overlay").hidden = true;
  }

  let latestData = null;
  let modalOpen = false;

  async function loadData({ silent } = {}) {
    try {
      const url = dataUrl();
      const sep = url.includes("?") ? "&" : "?";
      const res = await fetch(`${url}${sep}_=${Date.now()}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      orderRoundsByBracket(data);
      latestData = data;
      if (!modalOpen) renderBracket(data);
      return data;
    } catch (err) {
      if (!silent) {
        document.getElementById("bracket").innerHTML =
          `<p style="color:var(--critical)">Could not load match data (${escapeHTML(err.message)}).</p>`;
      }
      console.error("Failed to load match data:", err);
      return null;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    loadData();

    document.getElementById("modal-close").addEventListener("click", closeModal);
    document.getElementById("modal-overlay").addEventListener("click", (e) => {
      if (e.target.id === "modal-overlay") closeModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeModal();
    });
    document.getElementById("refresh-btn").addEventListener("click", () => loadData());

    // Track modal open/close so the 5-minute poll doesn't yank a card out
    // from under someone mid-read.
    const overlay = document.getElementById("modal-overlay");
    new MutationObserver(() => {
      modalOpen = !overlay.hidden;
    }).observe(overlay, { attributes: true, attributeFilter: ["hidden"] });

    setInterval(() => loadData({ silent: true }), REFRESH_MS);

    let resizeTimer;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (latestData && !modalOpen) drawConnectors(latestData);
      }, 150);
    });
  });
})();
