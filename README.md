# One-way fares from Florida for a family of 6

A static, single-page site showing the cheapest **one-way** economy fares for
**2 adults + 4 children** from Tampa, Orlando, Fort Lauderdale, West Palm Beach,
and Vero Beach to six destination groups:

| Group | Airports searched | Shown |
|---|---|---|
| Denver | DEN | DEN |
| California | LAX, SAN, SFO, OAK, BUR | cheapest |
| Cascades | SEA, PDX | cheapest |
| Maine | PWM, BGR | cheapest |
| Yellowstone | **JAC** (primary), BZN, IDA | cheapest, with the JAC price noted when another airport wins |
| Adirondacks | **ALB** (primary), SYR (backup) | cheapest, with the ALB price noted when SYR wins |

Cards are sorted by cheapest total for 6. Frontier gets a green badge when it is
the cheapest fare or within 10% of it. Expand a card to see every airport and
carrier option. Filters: origin, destination, "Frontier fares only", and "hide
routes with no data". The filter state is kept in the URL hash, so a link like
`#o=MCO&g=denver` works.

## Files

```
index.html  styles.css  app.js         static site (reads data/fares.json)
data/fares.json                         fare cache (written by the script)
config.json                             origins, groups, dates, carriers, delays
scripts/update_fares.py                 daily cache refresh
requirements.txt                        fast-flights (+ typing_extensions)
.github/workflows/refresh-fares.yml     daily schedule, commit, Pages deploy
serve_local.sh                          local preview on :8000
```

## How the daily refresh works

1. GitHub Actions runs `scripts/update_fares.py` every day at **10:00 UTC**
   (5 AM EST / 6 AM EDT). GitHub's scheduled runs can start 5 to 30 minutes late.
2. For each origin × airport pair (5 × 15 = **75 pairs**), the script searches
   Google Flights through the [`fast-flights`](https://github.com/AWeirdDev/fast-flights)
   library (no API key) for each departure date in the config. It uses a
   1-traveler search because Google's server-rendered page lists every carrier
   for one traveler, but for a 6-person search it usually lists only a few
   (often only Frontier).
3. It keeps the cheapest fare per carrier per pair. Then it re-searches the
   best date (and Frontier's best date) **for the real party of 2 adults +
   4 children**. When the same carrier appears in that party search, the site
   shows Google's actual total for 6, labeled `quoted_for_party`. Otherwise the
   total is per-person × 6 and the site marks it **"est."** because six seats at
   that fare aren't confirmed.
4. Results go to `data/fares.json`. The workflow commits that file back to the
   repo and deploys the site to GitHub Pages.
5. The page fetches `data/fares.json` on load. Nothing is scraped from the
   visitor's browser.

### Honest failure handling
- **Fetch failed** (blocked, captcha, network) for a pair: the previous run's
  data for that pair is kept with `status: "stale"`, its own `last_success`
  date, and the new `last_checked` time. The site shows a yellow "stale" badge.
  With no previous data, the pair becomes `status: "error"`.
- **Fetched, but no itineraries exist** (for example VRB, which only has
  Breeze nonstops to the Northeast): `status: "no_data"`. The route shows as a
  red "No fare data" card and is listed in `no_data_routes` in the JSON and in
  the banner at the top of the page.
- The script never makes up a price. Every number came from a live response
  on the date in `last_success`.
- If the whole cache is more than 36 hours old, the header warns that the
  refresh may be failing.

## Config (`config.json`)

| Key | Meaning |
|---|---|
| `origins`, `destination_groups` | Airports. `primary` is shown in the UI. Add a group by adding an object. |
| `passengers` | `adults`, `children` (Google allows a maximum of 9). |
| `dates.explicit` | List of `YYYY-MM-DD`. When non-empty (and in the future), only these dates are searched. |
| `dates.days_ahead` | Default `[21, 45, 75]`: three dates about 3, 6, and 10 weeks out. |
| `dates.snap_to_weekday` | Default `"Tue"`. Each date moves forward to the next Tuesday, usually a cheap day. `null` turns this off. |
| `search_code_aliases` | `{"PBI": "DJT"}`. Palm Beach International was renamed (President Donald J. Trump Intl) and Google Flights now returns nothing for `PBI`, only for `DJT`. The site still labels it PBI. Remove the alias if that changes. When Google returns no results but suggests nearby airports, the suggestion is saved in the pair's `notes`, which helps catch future code changes. |
| `tracked_carriers` | Code to display name (F9 Frontier, WN Southwest, NK Spirit, DL, AA, UA, AS, B6, MX). Other carriers still show under their Google name. |
| `frontier_competitive_pct` | Frontier badge threshold (default 10%). |
| `verify_party_price` | Re-search the best dates for the full party (default true). |
| `max_stops` | `null` = any, `0` = nonstop only, `1` = up to one stop. |
| `request_delay_seconds` | Random polite delay between requests (default 1.5 to 3.5 s). Failures retry 3× with backoff. |

**Request budget with the defaults:** 75 pairs × 3 dates = 225 searches, plus up
to 150 party re-checks. In practice about 300 searches and **about 20 minutes**
per daily run. Each extra date adds about 75 searches.

Local test helpers: `python scripts/update_fares.py --only TPA-DEN,MCO-JAC`
refreshes only those pairs (others carry over), `--limit 5`, `--dry-run`.

## Deploy on GitHub Pages

1. Create a repo and push this folder to `main`.
2. **Settings → Pages → Build and deployment → Source: "GitHub Actions".**
3. **Settings → Actions → General → Workflow permissions: "Read and write".**
   The workflow also declares `contents: write`, `pages: write`, `id-token: write`.
4. **Actions → "Refresh fares & deploy site" → Run workflow** once to publish.
   After that it runs daily. A push to `main` redeploys the existing cache
   without scraping.

### Secrets (all optional)
- `SERPAPI_KEY`: when set, up to `serpapi.max_calls_per_run` (default 3) pairs
  that failed on Google Flights are retried through SerpApi's Google Flights API
  for the full party. The free tier is about 100 searches/month, so keep this small.
- `FARES_PROXY`: an HTTP(S) proxy URL for the Google Flights requests (see
  caveats).

No secret is required for the default setup.

## Limitations and caveats
- **Google Flights may block GitHub Actions IPs.** It sometimes serves a
  consent or captcha page to datacenter IP ranges. When that happens, pairs go
  stale instead of showing wrong numbers. If it keeps happening, (a) add a
  `FARES_PROXY` secret (a residential proxy), (b) add `SERPAPI_KEY`, or (c) run
  the script daily from a home machine with cron (`0 6 * * *`) and push
  `data/fares.json`.
- `fast-flights` is an unofficial scraper. A Google page change can break it
  until the library or this parser is updated. That failure shows up as stale
  data, not fake data.
- **Child fares:** on US domestic flights, child fares are normally the same as
  adult fares. Per-person = total ÷ 6.
- Prices exclude bags and seat fees. This matters a lot on Frontier and Spirit.
- **Spirit stopped flying on May 2, 2026**, so it never shows up. `NK` stays in
  `tracked_carriers` but does nothing. **Southwest is included** in Google
  Flights results (WN appears in the data).
- Only the dates in the config are searched. A cheaper fare on another day
  won't show up.
- Mixed-carrier or self-transfer itineraries can appear under a combined name
  (for example "Frontier + Spirit").
