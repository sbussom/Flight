#!/usr/bin/env python3
"""Daily fare cache refresh.

Reads config.json, queries Google Flights (via the `fast-flights` library, no API
key) for every origin x destination-airport pair over a few departure dates, and
writes data/fares.json for the static site.

Rules:
  * Never invent numbers. Every price in fares.json came from a live response.
  * If a pair's fetch fails (blocked, network error, parse error), the previous
    run's data for that pair is kept and marked status="stale", with its own
    last_checked / last_success timestamps.
  * If a pair was fetched successfully but no itineraries exist, it is marked
    status="no_data".
  * Optional fallback: if the SERPAPI_KEY env var is set, a few failed pairs per
    run are retried through SerpApi's Google Flights engine.

Usage:  python scripts/update_fares.py [--limit N] [--only TPA-DEN,MCO-LAX] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
OUT_PATH = ROOT / "data" / "fares.json"
SCHEMA_VERSION = 1


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- dates
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def search_dates(cfg: dict, today: dt.date | None = None) -> list[str]:
    dcfg = cfg.get("dates", {})
    today = today or dt.date.today()
    explicit = [d for d in dcfg.get("explicit", []) if d >= today.isoformat()]
    if explicit:
        return sorted(set(explicit))
    snap = dcfg.get("snap_to_weekday")
    out = []
    for n in dcfg.get("days_ahead", [21, 45, 75]):
        d = today + dt.timedelta(days=int(n))
        if snap:
            target = WEEKDAYS.index(snap[:3].title())
            d += dt.timedelta(days=(target - d.weekday()) % 7)
        out.append(d.isoformat())
    return sorted(set(out))


# ------------------------------------------------------------------ google flights
class FetchError(Exception):
    pass


def _parse_google_html(html: str) -> list[dict]:
    """Parse the ds:1 data blob from a Google Flights results page.

    Reads both the "best" and "other" result blocks (fast-flights' own parser
    only reads one of them)."""
    from selectolax.lexbor import LexborHTMLParser

    node = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if node is None:
        low = html.lower()
        hint = "captcha/unusual-traffic page" if ("unusual traffic" in low or "captcha" in low) else (
            "consent page" if "consent" in low else "no results script")
        raise FetchError(f"Google Flights page not parseable ({hint})")
    js = node.text()
    try:
        data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    except IndexError as e:
        raise FetchError("unexpected ds:1 format") from e
    if data.endswith("errorHasStatus: true"):
        return [], None  # Google says: no flights
    payload = json.loads(data)
    out, seen = [], set()
    for idx in (2, 3):
        blk = payload[idx] if len(payload) > idx else None
        if not blk or not blk[0]:
            continue
        for k in blk[0]:
            try:
                f = k[0]
                price = k[1][0][1] if k[1] and k[1][0] else None
                if price is None:
                    continue
                segs = f[2] or []
                legs = []
                for s in segs:
                    fn = s[22] if len(s) > 22 and s[22] else None
                    legs.append({
                        "from": s[3], "to": s[6],
                        "dep": "%02d:%02d" % tuple((list(s[8] or []) + [0, 0])[:2]) if s[8] is not None else None,
                        "flight": f"{fn[0]}{fn[1]}" if fn else None,
                    })
                dep_t = f[5] or [0]
                arr_t = f[8] or [0]
                item = {
                    "carrier_code": f[0],
                    "carriers": f[1] or [],
                    "price": int(price),
                    "stops": max(len(segs) - 1, 0),
                    "depart_time": "%02d:%02d" % ((dep_t + [0, 0])[0] or 0, (dep_t + [0, 0])[1] or 0),
                    "arrive_time": "%02d:%02d" % ((arr_t + [0, 0])[0] or 0, (arr_t + [0, 0])[1] or 0),
                    "arrive_date": "%04d-%02d-%02d" % tuple(f[7]) if f[7] else None,
                    "duration_min": f[9],
                    "legs": legs,
                }
            except (IndexError, TypeError, ValueError):
                continue
            key = (item["carrier_code"], item["price"], item["depart_time"], tuple(l["flight"] for l in legs))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out, (None if out else _nearby_hint(payload))


def _nearby_hint(payload) -> str | None:
    """When Google has no results it sometimes suggests nearby airports, e.g.
    PBI -> "DJT 0 mi" after the airport's code change. Surface that as a note."""
    try:
        sugg = payload[6][0][4][1]
        parts = []
        for s in sugg[:4]:
            price = s[2][0][1] if s[2] and s[2][0] else None
            parts.append(f"{s[0]} ({s[4]}{', from $' + str(price) if price else ''})")
        return "Google found no flights; it suggests nearby airports (origin or destination side): " + ", ".join(parts) if parts else None
    except (IndexError, TypeError, KeyError):
        return None


def google_search(origin, dest, date, adults, children, cfg, proxy=None):
    """Returns (items, url, hint). Items' prices are the total for the party."""
    from fast_flights import FlightQuery, Passengers, create_query, fetch_flights_html

    q = create_query(
        flights=[FlightQuery(date=date, from_airport=origin, to_airport=dest)],
        trip="one-way",
        passengers=Passengers(adults=adults, children=children),
        currency="USD",
        language="en-US",
        max_stops=cfg.get("max_stops"),
    )
    last = None
    for attempt in range(int(cfg.get("retries", 3))):
        try:
            html = fetch_flights_html(q, proxy=proxy)
            items, hint = _parse_google_html(html)
            return items, q.url(), hint
        except Exception as e:  # network or parse
            last = e
            wait = (2 ** attempt) * 3 + random.uniform(0, 2)
            log(f"    retry {attempt + 1} {origin}-{dest} {date}: {e!s:.120} (sleep {wait:.0f}s)")
            time.sleep(wait)
    raise FetchError(str(last))


# ------------------------------------------------------------------------- serpapi
def serpapi_search(origin, dest, date, adults, children, key):
    params = {
        "engine": "google_flights", "departure_id": origin, "arrival_id": dest,
        "outbound_date": date, "type": "2", "adults": adults, "children": children,
        "currency": "USD", "hl": "en", "api_key": key,
    }
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.load(r)
    if "error" in data and not data.get("best_flights") and not data.get("other_flights"):
        if "no results" in str(data["error"]).lower():
            return []
        raise FetchError("SerpApi: " + str(data["error"]))
    out = []
    for f in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        segs = f.get("flights") or []
        if not segs or f.get("price") is None:
            continue
        names = sorted({s.get("airline", "") for s in segs})
        codes = sorted({(s.get("flight_number") or "  ")[:2] for s in segs})
        out.append({
            "carrier_code": codes[0] if len(codes) == 1 else "multi",
            "carriers": names,
            "price": int(f["price"]),  # SerpApi price is the total for all passengers
            "price_is_party_total": True,
            "stops": len(segs) - 1,
            "depart_time": (segs[0].get("departure_airport", {}).get("time") or "")[-5:],
            "arrive_time": (segs[-1].get("arrival_airport", {}).get("time") or "")[-5:],
            "arrive_date": (segs[-1].get("arrival_airport", {}).get("time") or "")[:10] or None,
            "duration_min": f.get("total_duration"),
            "legs": [{"from": s.get("departure_airport", {}).get("id"), "to": s.get("arrival_airport", {}).get("id"),
                      "dep": (s.get("departure_airport", {}).get("time") or "")[-5:],
                      "flight": (s.get("flight_number") or "").replace(" ", "")} for s in segs],
        })
    return out


# ------------------------------------------------------------------------ helpers
def carrier_name(item, tracked):
    code = item["carrier_code"]
    if code in tracked:
        return tracked[code]
    if item["carriers"]:
        return " + ".join(item["carriers"])
    return code


def summarize_pair(origin, dest, results_by_date, party_quotes, tracked, party_size):
    """results_by_date: {date: [items from a 1-adult search]} -> option dict."""
    best_by_carrier: dict[str, dict] = {}
    for date, items in results_by_date.items():
        for it in items:
            code = it["carrier_code"]
            cur = best_by_carrier.get(code)
            if cur is None or it["price"] < cur["price_pp"] or (
                    it["price"] == cur["price_pp"] and it["stops"] < cur["stops"]):
                best_by_carrier[code] = {
                    "carrier_code": code,
                    "carrier": carrier_name(it, tracked),
                    "is_frontier": code == "F9",
                    "date": date,
                    "price_pp": it["price"],
                    "stops": it["stops"],
                    "depart_time": it["depart_time"],
                    "arrive_time": it["arrive_time"],
                    "duration_min": it["duration_min"],
                    "legs": it["legs"],
                }
    fares = []
    for code, b in best_by_carrier.items():
        # Was the same carrier quoted for the whole party on that date?
        pq = [q for q in party_quotes.get(b["date"], []) if q["carrier_code"] == code]
        if pq:
            q = min(pq, key=lambda x: x["price"])
            b["total"] = q["price"]
            b["price_pp"] = round(q["price"] / party_size)
            b["total_basis"] = "quoted_for_party"
            b.update({k: q[k] for k in ("stops", "depart_time", "arrive_time", "duration_min", "legs")})
        else:
            b["total"] = b["price_pp"] * party_size
            b["total_basis"] = "per_person_x_party"
        fares.append(b)
    fares.sort(key=lambda x: (x["total"], x["stops"]))
    return fares


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only process the first N pairs (testing)")
    ap.add_argument("--only", default="", help="comma list like TPA-DEN,MCO-LAX")
    ap.add_argument("--dry-run", action="store_true", help="don't write fares.json")
    args = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text())
    tracked = cfg.get("tracked_carriers", {})
    adults = int(cfg["passengers"]["adults"])
    children = int(cfg["passengers"]["children"])
    party = adults + children
    dates = search_dates(cfg)
    proxy = os.environ.get("FARES_PROXY") or None
    serp_key = os.environ.get(cfg.get("serpapi", {}).get("enabled_if_env", "SERPAPI_KEY"), "")
    serp_budget = int(cfg.get("serpapi", {}).get("max_calls_per_run", 3)) if serp_key else 0
    dmin, dmax = cfg.get("request_delay_seconds", [1.5, 3.5])

    prev = {}
    if OUT_PATH.exists():
        try:
            old = json.loads(OUT_PATH.read_text())
            for r in old.get("routes", []):
                for o in r.get("options", []):
                    prev[(r["origin"], o["dest"])] = o
        except Exception as e:
            log("could not read previous fares.json:", e)

    only = {p.strip().upper() for p in args.only.split(",") if p.strip()}
    run_started = now_iso()
    log(f"Run {run_started}  dates={dates}  party={adults}A+{children}C  proxy={'yes' if proxy else 'no'}  serpapi={'yes' if serp_key else 'no'}")

    alias = cfg.get("search_code_aliases", {})

    def scode(code):
        return alias.get(code, code)

    pairs = [(o["code"], g, a) for o in cfg["origins"] for g in cfg["destination_groups"] for a in g["airports"]]
    if only:
        pairs = [p for p in pairs if f"{p[0]}-{p[2]}" in only]
    if args.limit:
        pairs = pairs[: args.limit]

    options: dict[tuple[str, str], dict] = {}
    stats = {"requests": 0, "ok": 0, "no_data": 0, "stale": 0, "error": 0}
    failed = []

    for i, (origin, group, dest) in enumerate(pairs, 1):
        checked = now_iso()
        results_by_date, errors, url_by_date, notes = {}, [], {}, []
        for date in dates:
            try:
                items, url, hint = google_search(scode(origin), scode(dest), date, 1, 0, cfg, proxy)
                stats["requests"] += 1
                if hint and hint not in notes:
                    notes.append(hint)
                results_by_date[date] = items
                url_by_date[date] = url
            except Exception as e:
                stats["requests"] += 1
                errors.append(f"{date}: {e!s:.200}")
            time.sleep(random.uniform(dmin, dmax))

        party_quotes = {}
        if results_by_date and cfg.get("verify_party_price", True):
            prelim = summarize_pair(origin, dest, results_by_date, {}, tracked, party)
            verify_dates = []
            if prelim:
                verify_dates.append(prelim[0]["date"])
                fr = next((f for f in prelim if f["is_frontier"]), None)
                if fr and fr["date"] not in verify_dates:
                    verify_dates.append(fr["date"])
            for date in verify_dates:
                try:
                    items, url, _ = google_search(scode(origin), scode(dest), date, adults, children, cfg, proxy)
                    stats["requests"] += 1
                    party_quotes[date] = items
                    url_by_date[date] = url  # prefer the party-size link
                except Exception as e:
                    stats["requests"] += 1
                    errors.append(f"party-verify {date}: {e!s:.200}")
                time.sleep(random.uniform(dmin, dmax))

        source = "google_flights (fast-flights)"
        if not results_by_date and serp_budget > 0:
            serp_budget -= 1
            try:
                items = serpapi_search(scode(origin), scode(dest), dates[0], adults, children, serp_key)
                party_quotes = {dates[0]: items}
                results_by_date = {dates[0]: [dict(x, price=round(x["price"] / party)) for x in items]}
                source = "serpapi google_flights (fallback)"
            except Exception as e:
                errors.append(f"serpapi: {e!s:.200}")

        if results_by_date:
            fares = summarize_pair(origin, dest, results_by_date, party_quotes, tracked, party)
            for f in fares:
                f["search_url"] = url_by_date.get(f["date"])
            status = "ok" if fares else "no_data"
            opt = {
                "dest": dest, "group": group["id"], "status": status,
                "last_checked": checked, "last_success": checked,
                "dates_searched": sorted(results_by_date), "source": source,
                "errors": errors, "notes": notes, "fares": fares,
            }
            if scode(origin) != origin or scode(dest) != dest:
                opt["searched_as"] = f"{scode(origin)}-{scode(dest)}"
        else:
            p = prev.get((origin, dest))
            if p and p.get("fares"):
                opt = dict(p, status="stale", last_checked=checked, errors=errors)
            else:
                opt = {
                    "dest": dest, "group": group["id"], "status": "error",
                    "last_checked": checked, "last_success": (p or {}).get("last_success"),
                    "dates_searched": [], "source": None, "errors": errors, "fares": [],
                }
            failed.append(f"{origin}-{dest}")
        stats[opt["status"]] = stats.get(opt["status"], 0) + 1
        options[(origin, dest)] = opt
        best = opt["fares"][0] if opt["fares"] else None
        log(f"[{i}/{len(pairs)}] {origin}-{dest}: {opt['status']}"
            + (f"  best {best['carrier']} ${best['total']} ({best['total_basis']}) {best['date']}" if best else "")
            + (f"  errors={len(errors)}" if errors else ""))

    # pairs not processed this run (e.g. --only/--limit): carry previous data unchanged
    for o in cfg["origins"]:
        for g in cfg["destination_groups"]:
            for a in g["airports"]:
                if (o["code"], a) not in options and (o["code"], a) in prev:
                    options[(o["code"], a)] = prev[(o["code"], a)]

    # status counts over every pair in the output (including carried-over ones)
    for k in ("ok", "no_data", "stale", "error"):
        stats[k] = sum(1 for op in options.values() if op.get("status") == k)
    stats["pairs_this_run"] = len(pairs)

    pct = float(cfg.get("frontier_competitive_pct", 10))
    routes = []
    for o in cfg["origins"]:
        for g in cfg["destination_groups"]:
            opts = [options[(o["code"], a)] for a in g["airports"] if (o["code"], a) in options]
            for a in g["airports"]:
                if (o["code"], a) not in options:
                    opts.append({"dest": a, "group": g["id"], "status": "not_checked", "last_checked": None,
                                 "last_success": None, "dates_searched": [], "source": None, "errors": [], "fares": []})
            cands = [dict(f, dest=op["dest"], option_status=op["status"]) for op in opts for f in op["fares"]]
            cands.sort(key=lambda x: (x["total"], x["stops"]))
            best = cands[0] if cands else None
            fr = next((c for c in cands if c["is_frontier"]), None)
            frontier = None
            if fr and best:
                gap = (fr["total"] - best["total"]) / best["total"] * 100 if best["total"] else 0
                frontier = {"dest": fr["dest"], "total": fr["total"], "price_pp": fr["price_pp"], "date": fr["date"],
                            "stops": fr["stops"], "pct_above_best": round(gap, 1),
                            "competitive": gap <= pct, "is_best": fr is best}
            statuses = {op["status"] for op in opts}
            if best is None:
                rstatus = "no_data"
            elif best["option_status"] == "stale":
                rstatus = "stale"
            else:
                rstatus = "ok"
            checked_vals = [op["last_checked"] for op in opts if op.get("last_checked")]
            routes.append({
                "origin": o["code"], "group": g["id"], "group_name": g["name"], "primary": g.get("primary"),
                "status": rstatus, "airport_statuses": {op["dest"]: op["status"] for op in opts},
                "has_stale": "stale" in statuses, "has_errors": bool(statuses & {"error", "not_checked"}),
                "last_checked": max(checked_vals) if checked_vals else None,
                "best": best, "frontier": frontier, "options": opts,
            })

    out = {
        "schema": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "run_started_at": run_started,
        "source": "Google Flights via fast-flights (no API key)" + ("; SerpApi fallback enabled" if serp_key else ""),
        "passengers": {"adults": adults, "children": children, "total": party},
        "trip": "one-way, economy",
        "dates_searched": dates,
        "frontier_competitive_pct": pct,
        "groups": cfg["destination_groups"],
        "origins": cfg["origins"],
        "airport_names": cfg.get("airport_names", {}),
        "search_code_aliases": cfg.get("search_code_aliases", {}),
        "stats": stats,
        "failed_pairs": failed,
        "no_data_routes": [f"{r['origin']}-{r['group']}" for r in routes if r["status"] == "no_data"],
        "routes": routes,
    }
    log(f"Done. stats={stats} failed={failed}")
    if args.dry_run:
        print(json.dumps(out, indent=1)[:4000])
        return 0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1))
    tmp.replace(OUT_PATH)
    # Exit 0 even with failures: stale data is still published honestly.
    return 0


if __name__ == "__main__":
    sys.exit(main())
