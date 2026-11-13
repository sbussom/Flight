(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const money = (n) => "$" + Math.round(n).toLocaleString("en-US");
  const fmtTime = (iso) => iso ? new Date(iso).toLocaleString("en-US", {
    timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short"
  }) : "never";
  const fmtDate = (d) => d ? new Date(d + "T12:00:00Z").toLocaleDateString("en-US", {
    timeZone: "UTC", weekday: "short", month: "short", day: "numeric", year: "numeric"
  }) : "";
  const ageHours = (iso) => iso ? (Date.now() - new Date(iso).getTime()) / 3.6e6 : Infinity;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const stopsTxt = (n) => n === 0 ? "nonstop" : `${n} stop${n > 1 ? "s" : ""}`;
  const dur = (m) => m ? `${Math.floor(m / 60)}h ${m % 60}m` : "";

  let DATA = null;
  const state = { origin: "", group: "", frontier: false, hideEmpty: false };

  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    state.origin = p.get("o") || ""; state.group = p.get("g") || "";
    state.frontier = p.get("f") === "1"; state.hideEmpty = p.get("h") === "1";
  }
  function writeHash() {
    const p = new URLSearchParams();
    if (state.origin) p.set("o", state.origin);
    if (state.group) p.set("g", state.group);
    if (state.frontier) p.set("f", "1");
    if (state.hideEmpty) p.set("h", "1");
    history.replaceState(null, "", "#" + p.toString());
  }

  function apName(code) {
    const n = (DATA.airport_names || {})[code] || code;
    const a = (DATA.search_code_aliases || {})[code];
    return a ? `${n} (searched as ${a})` : n;
  }

  // The fare to headline for a route under the current mode.
  function headline(route) {
    if (!state.frontier) return route.best;
    const all = route.options.flatMap((o) => o.fares.map((f) => ({ ...f, dest: o.dest, option_status: o.status })));
    return all.filter((f) => f.is_frontier).sort((a, b) => a.total - b.total)[0] || null;
  }

  function badges(route, fare) {
    const b = [];
    const fr = route.frontier;
    if (fare && fare.is_frontier && fr && fr.is_best) b.push(`<span class="badge fr">★ Frontier is cheapest</span>`);
    else if (!state.frontier && fr && fr.competitive) b.push(`<span class="badge fr">Frontier within ${fr.pct_above_best}% · ${money(fr.total)}</span>`);
    else if (!state.frontier && fr) b.push(`<span class="badge">Frontier ${money(fr.total)} (+${fr.pct_above_best}%)</span>`);
    if (fare && fare.total_basis === "per_person_x_party") b.push(`<span class="badge warn" title="Google quoted this carrier for 1 traveler; total = per-person × 6. Six seats at this fare not confirmed.">est. total (1-seat fare × 6)</span>`);
    if (fare && fare.option_status === "stale") b.push(`<span class="badge warn">stale – last good data ${fmtTime(route.options.find(o=>o.dest===fare.dest)?.last_success)}</span>`);
    if (route.primary && fare && fare.dest !== route.primary) {
      const p = route.options.find((o) => o.dest === route.primary);
      const pf = p && p.fares[0];
      b.push(`<span class="badge">Primary ${esc(route.primary)}: ${pf ? money(pf.total) : "no fare found"}</span>`);
    }
    if (route.has_errors) b.push(`<span class="badge bad">some airports failed to load</span>`);
    return b.join("");
  }

  function optionsTable(route) {
    const rows = [];
    for (const o of route.options) {
      const label = `${esc(o.dest)} <small>${esc(apName(o.dest))}${route.primary === o.dest ? " · primary" : ""}</small>`;
      if (!o.fares.length) {
        const why = o.status === "no_data" ? "No itineraries found" : o.status === "error" ? "Fetch failed: " + esc((o.errors || [])[0] || "") : o.status;
        const note = (o.notes || [])[0] ? `<br><small>${esc(o.notes[0])}</small>` : "";
        rows.push(`<tr><td>${label}</td><td colspan="5"><span class="badge bad">${why}</span> <small>checked ${fmtTime(o.last_checked)}</small>${note}</td></tr>`);
        continue;
      }
      const shown = state.frontier ? o.fares.filter((f) => f.is_frontier) : o.fares;
      if (!shown.length) { rows.push(`<tr><td>${label}</td><td colspan="5">No Frontier fare</td></tr>`); continue; }
      for (const f of shown) {
        rows.push(`<tr class="${f.is_frontier ? "frow" : ""}"><td>${label}</td>
          <td>${esc(f.carrier)}<br><small>${stopsTxt(f.stops)}${f.duration_min ? " · " + dur(f.duration_min) : ""}</small></td>
          <td>${fmtDate(f.date)}<br><small>dep ${esc(f.depart_time)}</small></td>
          <td class="r">${money(f.price_pp)}</td>
          <td class="r"><b>${money(f.total)}</b>${f.total_basis === "per_person_x_party" ? " <small>est.</small>" : ""}</td>
          <td class="hide-sm">${f.search_url ? `<a href="${esc(f.search_url)}" target="_blank" rel="noopener">view</a>` : ""}</td></tr>`);
      }
      if (o.status === "stale") rows.push(`<tr><td colspan="6"><span class="badge warn">${esc(o.dest)} data is stale: last successful check ${fmtTime(o.last_success)}</span></td></tr>`);
    }
    return `<details><summary>All airports &amp; carriers (${route.options.length} airport${route.options.length > 1 ? "s" : ""})</summary>
      <table><thead><tr><th>Airport</th><th>Airline</th><th>Date</th><th class="r">Per person</th><th class="r">Total (6)</th><th class="hide-sm"></th></tr></thead>
      <tbody>${rows.join("")}</tbody></table></details>`;
  }

  function card(route) {
    const fare = headline(route);
    const group = DATA.groups.find((g) => g.id === route.group);
    const dests = fare ? `${fare.dest} · ${apName(fare.dest)}` : group.airports.join(" / ");
    if (!fare) {
      const msg = state.frontier && route.best ? "No Frontier fare found on this route" : "No fare data retrieved for this route";
      return `<article class="card empty"><div class="row">
        <div class="route">${esc(route.origin)} → ${esc(route.group_name)}<small>${esc(apName(route.origin))} → ${esc(dests)}</small></div>
        <div class="airline" style="grid-column:span 3"><span class="badge bad">⚠ ${msg}</span>
          <small>Airports checked: ${route.options.map((o) => `${o.dest} (${o.status.replace("_", " ")})`).join(", ")}</small></div></div>
        <div class="meta">Last checked ${fmtTime(route.last_checked)}</div>
        ${optionsTable(route)}</article>`;
    }
    const opt = route.options.find((o) => o.dest === fare.dest);
    return `<article class="card ${fare.is_frontier ? "frontier" : ""}">
      <div class="row">
        <div class="route">${esc(route.origin)} → ${esc(route.group_name)}<small>${esc(apName(route.origin))} → ${esc(dests)}</small></div>
        <div class="airline"><span class="lbl">Airline</span>${esc(fare.carrier)}<small>${stopsTxt(fare.stops)} · ${fmtDate(fare.date)}</small></div>
        <div class="num pp"><span class="lbl">Per person</span>${money(fare.price_pp)}</div>
        <div class="num total"><span class="lbl">Total for 6</span>${money(fare.total)}</div>
      </div>
      <div class="badges">${badges(route, fare)}</div>
      <div class="meta">Last updated ${fmtTime(opt && opt.last_success)}${opt && opt.last_checked !== opt.last_success ? ` · last checked ${fmtTime(opt.last_checked)}` : ""}
        ${fare.search_url ? ` · <a href="${esc(fare.search_url)}" target="_blank" rel="noopener">see on Google Flights</a>` : ""}</div>
      ${optionsTable(route)}
    </article>`;
  }

  function render() {
    writeHash();
    let routes = DATA.routes.filter((r) => (!state.origin || r.origin === state.origin) && (!state.group || r.group === state.group));
    const withFare = routes.map((r) => ({ r, f: headline(r) }));
    let shown = withFare.filter((x) => x.f || !state.hideEmpty);
    shown.sort((a, b) => (a.f ? a.f.total : Infinity) - (b.f ? b.f.total : Infinity) || a.r.origin.localeCompare(b.r.origin));
    $("#list").innerHTML = shown.length ? shown.map((x) => card(x.r)).join("") : `<p class="meta">No routes match these filters.</p>`;
  }

  function renderHeader() {
    const p = DATA.passengers;
    $("#sub").textContent = `${p.adults} adults + ${p.children} children (${p.total} travelers) · ${DATA.trip} · departure dates scanned: ${DATA.dates_searched.map(fmtDate).join(", ")} (cheapest kept)`;
    const age = ageHours(DATA.generated_at);
    $("#updated").innerHTML = `Last refresh: <b>${fmtTime(DATA.generated_at)}</b>` + (age > 36 ? ` <span class="badge warn">over ${Math.floor(age / 24)} day(s) old: the daily refresh may be failing</span>` : "");
    const alerts = [];
    const noData = DATA.routes.filter((r) => r.status === "no_data");
    if (noData.length) alerts.push(`<div class="alert bad"><b>No data for ${noData.length} route${noData.length > 1 ? "s" : ""}:</b> ${noData.map((r) => `${r.origin} → ${r.group_name}`).join(", ")}. These are shown at the bottom of the list.</div>`);
    const stale = DATA.routes.filter((r) => r.has_stale);
    if (stale.length) alerts.push(`<div class="alert warn"><b>Stale data on ${stale.length} route${stale.length > 1 ? "s" : ""}</b> (last refresh couldn't reach the source, so the previous result is shown with its date): ${stale.map((r) => `${r.origin} → ${r.group_name}`).join(", ")}.</div>`);
    $("#alerts").innerHTML = alerts.join("");
    $("#foot").textContent = `Source: ${DATA.source}. Run stats: ${DATA.stats.requests} searches, ${DATA.stats.ok} airport pairs OK, ${DATA.stats.no_data} with no itineraries, ${DATA.stats.stale || 0} stale, ${DATA.stats.error || 0} failed.`;
  }

  function initControls() {
    const o = $("#f-origin"), g = $("#f-group");
    DATA.origins.forEach((x) => o.insertAdjacentHTML("beforeend", `<option value="${x.code}">${x.code} · ${esc(x.name)}</option>`));
    DATA.groups.forEach((x) => g.insertAdjacentHTML("beforeend", `<option value="${x.id}">${esc(x.name)} (${x.airports.map((a) => a === x.primary && x.airports.length > 1 ? a + "*" : a).join("/")})</option>`));
    o.value = state.origin; g.value = state.group; $("#f-frontier").checked = state.frontier; $("#f-hide-empty").checked = state.hideEmpty;
    o.onchange = () => { state.origin = o.value; render(); };
    g.onchange = () => { state.group = g.value; render(); };
    $("#f-frontier").onchange = (e) => { state.frontier = e.target.checked; render(); };
    $("#f-hide-empty").onchange = (e) => { state.hideEmpty = e.target.checked; render(); };
  }

  readHash();
  fetch("data/fares.json?v=" + Date.now(), { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then((d) => { DATA = d; initControls(); renderHeader(); render(); })
    .catch((e) => {
      $("#updated").innerHTML = `<span class="badge bad">Could not load data/fares.json (${esc(e.message)}). The daily refresh may not have run yet.</span>`;
    });
})();
