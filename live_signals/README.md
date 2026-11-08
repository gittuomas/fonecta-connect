# Live context in the existing hours flow

**Live events and weather help Connect choose when to ask. The owner decides the hours. Yext saves the approved change.**

This is the chosen live-source extension: Nordic coverage, selective contact, owner-confirmed hours, and the existing approval/edit/decline/UNDO flow. It adds no dashboard and makes no change to C13's frozen predictions or the team's holiday messages.

## Exact requirement and what works

The challenge asks us to **replace at least one dataset file with a live source and report coverage, quality and gaps**. It does not require a higher model score. `local_events.json` is replaced at the event-check input boundary: the selector reads real listings through `LiveContext`. `weather_daily.csv` is replaced at the ski-rental weather-check boundary; Open-Meteo forecasts determine whether a weather question is relevant. For other businesses weather remains operator context. Neither source supplies new hours. The original materials remain read-only.

| Component | Role |
|---|---|
| `providers.py`, `datasets.py` | Fetch Nordic events and forecasts; preserve source timestamps, missing values, attribution and checksums. |
| `context.LiveContext` | Validate the replacement files; exclude stale/cancelled events and historical look-ahead. |
| `checks.py` | Select `CHECK_HOURS` questions, with `proposed_hours: null`. Rank eligible businesses using their observed search activity when available. |
| `owner.py`, `owner-page/app.py`, `owner-page/proactive.py` | Add event checks to the existing channel screens and feed live forecasts into the ski-rental check. Current listed hours are a confirmation choice. Owner preferences apply. |
| `step5_apply` | Apply only owner-confirmed/edited hours; verify, audit, confirm and undo through the existing path. |
| Selected C13 engine | Unchanged benchmark predictor. Live checks neither overwrite its decisions nor become scored benchmark rows. |

The end-to-end demo has passed with the local mock and a real mapped Yext sandbox: owner edit → write → read-back → UNDO → restoration read-back. Contact is still simulated; no external message is sent. Recorded replay refuses a configured sandbox.

## Run from a clean main checkout

Use **Python 3.12+** for the owner app. From the repository root:

```powershell
python -m pip install -r live_signals/requirements.txt
# Optional: rank by observed business search activity; otherwise use proximity.
python -m pip install pyarrow
# Copy .env.example to ignored .env and fill in keys first.
python -m live_signals.demo --sandbox --runtime-dir owner-page/.runtime/rehearsal-01
```

Open `http://localhost:8000`. Selected live-check businesses appear in the existing panel with source links and per-message sandbox status. Events use the existing WhatsApp, SMS, email, push and web screens; weather/season use the WhatsApp/web flow. The ordinary holiday messages are unchanged. The demo does not implement a delivery scheduler for live checks; contact-window delivery remains a production integration task.

Omit `--sandbox` to force local-only writes. `--prepare-only` refreshes/selects and performs read-only Yext preflight; `--use-existing` keeps the current bundle but still enforces freshness. `--env-file <path>` loads credentials from an ignored file in another checkout. The separate `refresh` and `checks` commands remain available; set `CONNECT_LIVE_CHECKS` to their checks export when launching `owner-page/app.py` manually.

`refresh` writes the two replacement files and `manifest.json` into the ignored `live_signals/.runtime/context/`. Default window: the next seven days; all 28 challenge cities are requested for weather. `--days` allows 1–10, `--max-pages` 1–9, and `--output` chooses a separate directory. Protected challenge materials and recorded evidence cannot be overwritten, including through `--output`.

`checks` defaults to at most three businesses, selecting across FI/SE/NO before filling remaining slots. Use `--limit 1..20`, `--bundle <directory>`, `--output <file>` or `--no-analytics`. Restart the owner app after regenerating the export. Refresh and regenerate before demonstrating: unopened checks expire when source evidence is over an hour old. An empty selection is an honest result, not a reason to fall back to synthetic events.

For reproducible offline demonstration, use the committed public-feed recording:

```powershell
$bundle = 'live_signals/data/context_2026-11-07'
$cutoff = (Get-Content "$bundle/manifest.json" -Raw | ConvertFrom-Json).generated_at
python -m live_signals checks --bundle $bundle --as-of $cutoff --output live_signals/.runtime/replay.json
$env:CONNECT_LIVE_CHECKS = (Resolve-Path live_signals/.runtime/replay.json).Path
# Disable sandbox mirroring for an explicitly backdated replay.
$env:YEXT_SANDBOX_MIRROR = 'disabled'
python owner-page/app.py
```

The operator panel labels **recorded_replay**; it is not current live evidence. This recording excludes Ticketmaster content, so it may select fewer countries than a fresh run with a key. Synthetic challenge businesses still describe the 6 November snapshot. Neither mode is a production hours predictor.

Inspect source facts without selecting a check:

```sh
python -m live_signals inspect FI-00925 2026-11-13
```

## When we ask, and when we stay quiet

The current selection policy is a **reviewable relevance hypothesis**, not a calibrated demand model:

- Match a café, bakery, restaurant, pizzeria or bar to a timed music, concert, festival or sports event in the same country, within 750 metres.
- Event start must be 30 minutes before to 90 minutes after the business's usual closing time, with at least 24 hours' lead time. Require a named venue and fresh, uncancelled evidence.
- Exclude nursery/storytime activities and ticket add-ons such as parking/VIP packages. Proximity by itself is insufficient.
- Keep current listing hours as the confirmation choice. Never infer an earlier opening, later closing, attendance or event-driven demand probability. Unknown event end times stay unknown.
- C13 SUGGEST decisions on the same date take priority; any C13 FLAG excludes that business. Closed businesses, opted-out contacts and dates with owner-entered exception hours are excluded.
- Rank eligible checks by the business's **observed weekday Google search views** over an eight-week window before the frozen snapshot, with a two-day availability lag. These are synthetic historical challenge observations, not predicted event footfall. Missing analytics stays unknown and falls back to proximity.
- One selected check per business per run; once displayed, a persisted 14-day limit suppresses different live checks for that business. No reminders or voice escalation for these speculative checks.
- Existing STOP, quiet mode, reason preferences and prior responses suppress new checks. Confirm/edit/decline/UNDO feed the existing event-preference learning. Web decisions are included. Annual standing permission is never granted for a live event.

For example, a concert at 19:00 near a café listed to close at 18:00 can justify: **“Are your listed hours right for that day?”** The owner can confirm 10–18, enter 10–20, or mark the event irrelevant. Only the owner's decision can produce a dated listing update.

The rejected **“Pienten lorutuokio at 09:30 → open at 08:30”** case stays silent, even if a nursery event is moved near closing time. A small nearby activity is not evidence that the café should change its hours.

### Weather in the existing check

Live forecasts replace the synthetic weather input for ski rentals only. the existing question, approval/edit/decline controls and per-owner snowfall thresholds are retained. Daily `snowfall_sum` (cm) drives fresh-snow checks; local-noon `snow_depth` (metres converted to cm) describes settled snow and low-snow checks. The replacement CSV preserves its original columns and appends optional `snowfall_cm`. Old recordings without that field have unknown snowfall, not zero and not a depth-change estimate.

Missing or expired live forecasts produce no weather question, without fallback to the synthetic fixture. C13 actions for the same date and FLAG/closed/opted-out businesses take priority. Confirming writes only the displayed hours; changed hours must come from the owner. These checks do not create annual permission. The event-specific 14-day ledger does not yet deduplicate weather questions across process restarts. With live mode disabled, the original synthetic weather demonstration remains available.

Before opening a new check, the adapter re-reads source evidence and suppresses cancellation, removal, checksum failure and expiry. Started conversations remain available for owner replies and UNDO. Provider omissions and unreported cancellations remain a source limitation.

## Verified coverage and gaps

The **7 November 2026, 14:25 UTC** online audit retrieved **944 distinct event occurrences** for 8–14 November: **740 FI, 167 SE, 37 NO**. It returned **196 usable city-days across all 28 cities**, with no missing weather values in that retrieval. All event providers returned live responses. Helsinki hit the eight-page cap, so the result is partial.

The check policy found **163 eligible business/event pairs across 57 businesses**, then selected three checks, one per country. This is a selection count, not measured precision, engagement or evidence that any hours are wrong. A fresh run was consumed by the owner app in live mode and verified in Chrome: all three mobile pages, owner edit, undo, and the existing history/no-history messages.

The distributable recording contains **891 events (725 FI, 147 SE, 19 NO)** and the same 196 weather rows. Its 53 Ticketmaster records have been removed from the current tree; original aggregate retrieval counts remain in the manifest. Ticketmaster data should be refreshed into the ignored runtime, not added as a permanent Git fixture.

| Source | Coverage, quality and limitations |
|---|---|
| [Helsinki Linked Events](https://api.hel.fi/linkedevents/v1/) | Helsinki region; series parents and unknown start times excluded. Explicit cancellation supported. The page cap makes coverage partial. |
| [Visit Stockholm](https://api.visitstockholm.com/documentation/) | Explicit scheduled occurrences with venue coordinates; range-only activities excluded. No dependable cancellation flag; removal appears on refresh. |
| [Tangokalender Oslo](https://tango.oslo.no/en/bygg-inn.html) | Narrow tango/community coverage. [Kartverket](https://ws.geonorge.no/adresser/v1) supplies approximate venue-address coordinates; ambiguous addresses excluded. This alone is not a useful national Norway feed. |
| [Ticketmaster Discovery](https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/) | Ticketed events across FI/SE/NO, including useful Norway concert cases. Not comprehensive; unknown time/position excluded, cancellation retained. Key required. |
| [Open-Meteo](https://open-meteo.com/en/docs) | Ten-day forecasts checked against the requested window. Daily aggregates use local time; daily snowfall is in cm. Snow depth is the local-noon forecast, metres converted to cm. These are forecasts, not observations. No key needed for the noncommercial demo. |

Attendance was unknown for every event in the audit. A missing source never means “no events”; missing weather is blank rather than zero. Cache hits retain their original retrieval times. Evidence older than one hour is excluded, and a retrieval after the decision cutoff is rejected. Real event IDs are never substituted for synthetic benchmark IDs. The manifest records source states, timestamps, limitations and file checksums.

## Verification and next validation

```sh
python -m unittest live_signals.test_live_signals live_signals.test_checks live_signals.test_demo -v
python -m unittest discover -s owner-page -p "test_*.py" -v
python -m unittest discover -s step5_apply/tests -t .
```

Tests cover useful checks versus nursery noise, timing/segment/geography, C13 precedence, cancellation/staleness, protected original inputs, cooldown persistence, preferences, owner-entered exceptions, confirm/edit/decline, repeat approval, source-linked audit, UNDO and prohibition of annual grants. These tests use controlled fixtures and the local mock, without network calls or real messaging.

Next: rehearse on the recording machine, then validate usefulness with owners. Measure checks accepted/edited as useful, irrelevant checks, opt-outs and actual incorrect-hours corrections. The event and weather thresholds need those outcomes before scaling. Production delivery scheduling, persistent weather deduplication and retraining the predictor remain future work. Historical context research did not establish an accuracy gain; do not present these live checks as an improvement to C13's reported score.

## Keys, attribution and terms

Register at [Ticketmaster Developer](https://developer-account.ticketmaster.com/user/register). Put the application's **Consumer Key** in the root, ignored `.env` as `TICKETMASTER_API_KEY=...`; no consumer secret is needed. The key is never included in exported source URLs, logs or bundles. Without it, public feeds and weather still work, with narrower coverage.

Helsinki and Stockholm events and Open-Meteo weather: **CC BY 4.0**; normalized facts retain source links and timestamps. Stockholm attribution: **Stockholm Business Region AB**. Oslo organizer links and Kartverket coordinate provenance are retained. [Ticketmaster terms](https://developer.ticketmaster.com/support/terms-of-use/) apply to its event content, including reasonable storage periods and removal requests; use runtime data only and clear it after the demo. Do not redistribute its event content as a permanent dataset. A commercial deployment needs a provider/terms review. [Open-Meteo's free endpoint](https://open-meteo.com/en/terms) is for noncommercial use.

Challenge data: **“Connect Suggests simulator, Since AI ry 2026.”** `tzdata` provides Windows timezone support (Apache-2.0 package; IANA data public domain). Optional `pyarrow` is Apache-2.0. No new UI dependency is introduced.
