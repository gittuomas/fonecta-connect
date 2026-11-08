# Step 5 (Apply): how steps 1–4 plug in

Step 5 takes the owner's answer from step 4, writes it to Yext, checks it landed, and returns the confirmation message to send back to the owner. You can call it from Python or over HTTP. Both use the same JSON.

## Start it

Run these from the repository root.

```sh
python -m step5_apply.service                                    # http://127.0.0.1:8790, own Yext mock
python -m step5_apply.service --mock-url http://127.0.0.1:8787   # if the team already runs mock_api/server.py
python -m step5_apply.service --no-sandbox                       # don't also write to the Yext sandbox
```

From Python, without HTTP:

```python
from step5_apply.api import Step5
s5 = Step5.from_env()          # same options: mock_url=..., use_sandbox=False, runtime_dir=...
s5.apply({...}); s5.undo(entity_id="FI-D0001"); s5.snapshot(...); s5.policy(...); s5.close()
```

- **Sandbox:** if `.env` has `YEXT_API_KEY` and `YEXT_SANDBOX_MIRROR=FI-D0001:Cinnamon Swirl Café`, every change applied to `FI-D0001` is also written to our real Yext sandbox business.
- **Saved state:** the audit log, standing permissions and the mock's state live in `step5_apply/.runtime/` (gitignored), so they survive restarts.
- **Reset for a fresh demo:** delete `step5_apply/.runtime/`.

## When to call what

| When (your step) | Call | Why |
|---|---|---|
| Step 2/3: you create a suggestion | `GET /snapshot?entity_id=FI-D0001&dates=2026-12-24,2026-12-31` | Store `seen_before` with the suggestion. Step 5 then won't overwrite a date the owner changed themselves in the meantime. |
| Step 3: before contacting | `GET /policy?entity_id=FI-D0012&date=2026-12-24&hours=closed` | `auto_apply: true` → skip asking and call `/apply` with `"actor": "standing_permission"` (step 5 informs the owner). `opted_out: true` → don't contact. Also returns preferred channel, language and contact window. |
| Step 3: optional preview | `GET /where?entity_id=FI-D0001` | Which channels will show the change and how fast. Good for the first message ("Google updates within minutes"). |
| Step 4: owner answered | `POST /apply` with the decision below | Writes, verifies, logs. Send `message` from the result to the owner on the same channel. |
| Step 4: any later free-text reply | `POST /reply {"entity_id", "text"}` **first** | If it's an undo word (PERU / ÅNGRA / ANGRE / UNDO), the latest change is undone and you get the undo message back. Otherwise you get `{"undo": false}` and handle the reply yourself. |
| Undo from a button | `POST /undo {"audit_id"}` or `{"entity_id"}` | Undoes that change, or the business's most recent one. |

## Decision (what you send to `/apply`)

```json
{
  "entity_id": "FI-D0001",
  "suggestion_id": "msg-2026-0042",
  "channel": "WHATSAPP",
  "actor": "owner",
  "remember_yearly": false,
  "language": "en",
  "items": [
    {"date": "2026-12-24", "hours": "closed",      "decision": "ACCEPTED", "seen_before": null},
    {"date": "2026-12-31", "hours": "10:00-14:00", "decision": "EDITED",   "seen_before": null},
    {"date": "2026-12-26",                          "decision": "REJECTED"}
  ]
}
```

| Field | Required | Notes |
|---|---|---|
| `entity_id` | yes | The mock business ID (`FI-D0001`, `FI-00123`…). `entityId` also works. |
| `items[].date` | yes | `YYYY-MM-DD`, each date once. |
| `items[].hours` | for ACCEPTED/EDITED | The **final** hours: what was suggested if accepted, the owner's version if edited. `closed`, `regular` (open as usual), `10:00-15:00`, `10:00-14:00,17:00-22:00`, `18:00-02:00`, `00:00-23:59` (24 h). A Yext object like `{"isClosed": true}` also works, as do `final_hours` from the owner simulator. Spaces and `9:00` are tolerated. |
| `items[].decision` | no (default ACCEPTED) | `ACCEPTED`, `EDITED`, `REJECTED`, `NO_RESPONSE`. Aliases work: approve/approved/yes, edit, decline/declined/no, timeout/expired. |
| `items[].seen_before` | recommended | The value from `/snapshot` when you sent the suggestion. `null` means there was no entry. **Leave the key out** to skip the conflict check. |
| `items[].day_key` | no | Filled in from the calendar (XMAS_EVE, NYE, EVENT…). |
| `suggestion_id` | recommended | Makes repeats safe: the same suggestion applied twice won't write twice or message twice. |
| `actor` | no | `owner` (default) or `standing_permission`. The latter only works when `/policy` says `auto_apply`. |
| `remember_yearly` | no | Owner ticked "every year": future suggestions for that holiday with the same hours get `auto_apply`. Not stored for one-off EVENT days. |
| `language` | no | Message language. **Default `en`** (team decision for the demo). `fi`, `sv`, `nb` also work, and `owner` uses the owner's CRM language. To change the default for every call, set `STEP5_LANGUAGE` (e.g. `owner`). |
| `channel` | no | Stored in the audit log. |

**Shorthand for one date:** put the item fields at the top level, e.g. `{"entity_id": "FI-D0001", "date": "2026-12-24", "hours": "closed"}`.

**Malformed input** gets HTTP 400 with `{"error": "..."}` (Python: `ValueError`). Everything else returns HTTP 200 with a status per date, and step 5 never crashes on a Yext problem.

## Result (what you get back)

```json
{
  "entity_id": "FI-D0001",
  "items": [
    {"date": "2026-12-24", "status": "APPLIED", "hours": "closed", "before": null, "reason": "", "duplicate": false}
  ],
  "applied": ["2026-12-24"],
  "audit_id": "f60ce1c36c8e",
  "message": "Done ✅ Kahvila Aurora:\n• 24 Dec closed\nShows on: Google (Search & Maps) ~2 min, ...\nWrong? Reply UNDO and we'll put it back.",
  "message_short": "Done ✅ Kahvila Aurora: 24 Dec closed. Live on Google ~2 min + 8 more. Reply UNDO to undo.",
  "mirror": {"entity_id": "Cinnamon Swirl Café", "ok": true, "...": "..."}
}
```

**`message`: send it to the owner as-is** when it isn't `null`. `null` means nothing to say (e.g. they declined, or a repeat request). **`message_short`** is the same news in one line (under 160 characters) for SMS, WhatsApp previews or a voice script.

| `status` | Meaning | What your step does |
|---|---|---|
| `APPLIED` | Written to Yext and read back correctly | Send `message`. Keep `audit_id` if you show an Undo button. |
| `ALREADY_SET` | Yext already had exactly this | Send `message` (says nothing needed changing). If `duplicate` is true it was a repeat, and `message` is null. |
| `CONFLICT` | The date changed after you sent the suggestion (owner did it themselves) | Send `message`. Their own value was kept. |
| `DECLINED` / `NO_RESPONSE` | Owner said no / didn't answer | Nothing written, nothing to send from step 5. |
| `BLOCKED` | Opted out, date already passed, or no standing permission | Don't retry. `reason` says which. |
| `INVALID` | Hours or date not understood | Ask the owner again. `reason` says what's wrong. |
| `FAILED` | Yext error, or the read-back didn't match | `message` tells the owner it's being handled. Show it to a human. Nothing half-written is left behind. |
| `UNDONE` | (from `/undo`) the old value is back | Send `message`. |

## How this fits the contact plan (`docs/contact-step-plan.md`)

| Contact plan says | Step 5 |
|---|---|
| English for the demo | Messages default to English (`language` / `STEP5_LANGUAGE` to change) |
| Always ask; nothing changes without a yes | Only `ACCEPTED`/`EDITED` from the owner writes. No reply = `NO_RESPONSE`, nothing changes. Send every owner answer with `"actor": "owner"` |
| Skip opted-out owners; stay quiet when Google is already right | `/policy` → `opted_out`. `/apply` returns `ALREADY_SET` and writes nothing if Yext already shows it |
| Closed for good → a Fonecta person | Don't send those to `/apply` |
| One message per holiday period | Put all the period's dates in one decision's `items`. Each date gets its own status, so a mixed answer works |
| Voice call: press 1 approve / 2 decline | Send `ACCEPTED` / `REJECTED` with `"channel": "VOICE"`. Read `message_short` back as the confirmation |
| Option: "handle holidays for me", one reply confirms all dates | Send `"remember_yearly": true` with the owner's yes. Next time `/policy` returns `learned_permission` per holiday, so step 3 can send the short "confirm all" message. The owner still says yes. (`"actor": "standing_permission"` exists for "apply first, then inform", but the team's plan doesn't use it) |
| To write: "Confirmation after a yes (where the new hours show and how fast)" | **Already done**: that's `message` / `message_short` from `/apply` |

## Rules step 5 enforces

- **The owner has the last word.** Nothing is written without an ACCEPTED/EDITED answer or a covering standing permission.
- **Don't send FLAG cases to `/apply`.** A business that has closed for good goes to a person.
- **One write per call, one write at a time per business.** Two answers arriving together can't overwrite each other.
- **Undo by reply only ever undoes the latest change.** A second UNDO won't roll back something older. If an undo only partly worked (one date failed), the next UNDO retries just the dates that are still left.
- **Sending the same decision again is safe, and it also retries the sandbox.** If the first write to the sandbox failed, a repeat with the same `suggestion_id` writes only what the sandbox is missing (`mirror.written: true`) and logs a `MIRROR` audit record. The primary isn't written twice.
- **Network trouble doesn't hide a write.** If Yext's answer is lost, step 5 reads the entity back: a change that landed is `APPLIED` (and can be undone), and one that didn't is `FAILED`.

## Try it with curl

```bash
curl -s "http://127.0.0.1:8790/snapshot?entity_id=FI-D0001&dates=2026-12-24"
```
```bash
curl -s -X POST http://127.0.0.1:8790/apply -H "Content-Type: application/json" -d '{"entity_id":"FI-D0001","suggestion_id":"demo-1","language":"en","items":[{"date":"2026-12-24","hours":"closed","decision":"ACCEPTED","seen_before":null}]}'
```
```bash
curl -s -X POST http://127.0.0.1:8790/reply -H "Content-Type: application/json" -d '{"entity_id":"FI-D0001","text":"UNDO","language":"en"}'
```

## Live context through Laura's owner app

`python -m live_signals.demo --sandbox` refreshes events and weather, read-verifies the configured sandbox and starts the existing owner app. See [the integration handoff](../live_signals/README.md). Preparation never applies hours. Only the existing owner decision handlers call Step 5. One-off event/weather/season checks set their own `day_key` and never set `remember_yearly`.

The example mapping includes the holiday hero `FI-D0001` and live event case `FI-00762`, both to sandbox entity `Cinnamon Swirl Café`. Rehearse sequentially. Other businesses are local-only unless explicitly mapped. The operator panel displays `mirror.ok` separately from the local result; an `APPLIED` local result alone does not prove a sandbox update or Google publication.

Sandbox UNDO restores the sandbox's **own before values**, not the mock's before values. It leaves pre-existing matching entries alone and refuses restoration if the target date has since changed or the mapping changed. A failed sandbox restoration is shown to the operator and needs attention; successful local UNDO alone does not prove sandbox restoration. Preserve the audit until read-back confirms the target is restored.

Verified in Chrome on 7 November 2026, 17:13 EET: owner-edited 14 November hours → real sandbox write/read-back → UNDO/read-back. All three pre-existing holiday entries and weekly hours were preserved. Automated mirror tests also cover different prior sandbox values, later edits and successful write retries.

## Using the owner simulator from the challenge package

`POST /sim/v1/owner-response` on the mock returns `{"decision", "final_hours", ...}`. Map it straight into an item:

```python
sim = owner_response("FI-D0001", "2026-12-24", "closed")      # examples/yext_client.py
item = {"date": "2026-12-24", "decision": sim["decision"], "final_hours": sim["final_hours"], "seen_before": None}
```

## Real WhatsApp destination

`Step5.from_env(mode="sandbox")` requires a Yext sandbox key and one-to-one `YEXT_SANDBOX_MIRROR` mappings. The primary destination is the mapped real sandbox entity; missing configuration fails without a simulator fallback. This mode is used by the real [WhatsApp transport](../owner-page/MESSAGING.md). The browser demo keeps its local simulator plus optional sandbox mirror.
