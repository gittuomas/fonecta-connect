# Connect Voice on ElevenLabs: recreate the agent

Everything that lives on the ElevenLabs platform for the Connect Voice agent, exported from the live agent on 8 Nov 2026. With these files and the voice service in [`voice/`](../), Fonecta can rebuild the agent in its own ElevenLabs workspace.

Removed from the export: the creator's account details, workspace, branch and version IDs, and our private tunnel address. Tool URLs use the placeholder `https://YOUR_PUBLIC_BASE_URL`. The export contains no API keys or secrets.

## Files

| File | What it is |
|---|---|
| [`agent_config.json`](agent_config.json) | The full agent configuration: conversation settings, prompt, tools, analysis, privacy. Importable. |
| [`system_prompt.md`](system_prompt.md) | The system prompt as live. |
| [`first_message.txt`](first_message.txt) | The first message as live. |
| [`tools/record_decision.json`](tools/record_decision.json), [`tools/set_standing_permission.json`](tools/set_standing_permission.json), [`tools/set_contact_preference.json`](tools/set_contact_preference.json) | The three webhook tools that call the voice service. |
| [`tools/system_tools.json`](tools/system_tools.json) | Built-in tools: `end_call`, `skip_turn`, `voicemail_detection`. |
| [`analysis.json`](analysis.json) | Post-call evaluation criteria and data collection fields. |
| [`dynamic_variables.json`](dynamic_variables.json) | Every dynamic variable with its test value. |

## Demo-only parts to change for production

The live agent was set up for the demo video, which covers only Christmas Eve:
- **Prompt:** the first block of `system_prompt.md`, "# Demo scope (temporary)", limits the call to Christmas Eve. Delete it to cover every suggested date.
- **First message:** it names Christmas Eve. For production, set it back to `{{greeting}}`. The voice service renders that greeting per business, naming the dates it will ask about.

## Recreate it

### 1. Run the voice service where ElevenLabs can reach it
The agent's tools are webhooks to the voice service, so it needs a public HTTPS address (a server, or ngrok for testing). See [`voice/README.md`](../README.md). Note that address; it replaces `https://YOUR_PUBLIC_BASE_URL` below.

### 2. Create the agent
**Quickest:**
1. In `agent_config.json`, replace every `https://YOUR_PUBLIC_BASE_URL` with your address.
2. In ElevenLabs, go to Agents, create a blank agent, open the "..." menu next to its name, choose **Import agent JSON config**, and paste the file.
3. Check that the three webhook tools appear under Tools with your URLs.

**By hand:** create a blank agent and set everything in the tables below.
1. Paste `system_prompt.md` as the system prompt and `first_message.txt` as the first message.
2. Add each webhook tool under Tools, then Add tool, using JSON mode with the matching file from `tools/`.
3. Switch on the system tools.

Whichever way you choose:
- **Test values:** every dynamic variable needs one, or the tools can't be saved. Copy them from `dynamic_variables.json`.
- **Publish:** nothing is live until you click Publish, then Publish again in the review dialog. Tool edits need a publish too.

### 3. Connect the service to the agent
Put the agent ID in the repository `.env` as `ELEVENLABS_AGENT_ID`, plus `PUBLIC_BASE_URL`. Add `ELEVENLABS_API_KEY` for phone calls or private agents, and `ELEVENLABS_WEBHOOK_SECRET` for the post-call webhook (`/webhooks/elevenlabs`). Then start a call from the voice console, http://localhost:8000.

## Settings

| Area | Setting |
|---|---|
| Voice mode | **Custom** (speech recognition, LLM and TTS as separate steps). Do not use Realtime: in our tests the Realtime models told the owner changes were saved without ever calling the tools. |
| LLM | `deepseek-v41-flash` (DeepSeek Flash 4.1), temperature 0, default reasoning, backup LLM default, cascade timeout 4 s, default personality ignored, parallel tool calls on, timezone Europe/Helsinki |
| Language | English (`en`) |
| Voice | Eric, voice ID `cjVigY5qzO86Huf0OWal`, TTS model `eleven_v4_turbo`, stability 0.5, similarity 0.75, speed 1.0, latency optimisation 3, PCM 16 kHz |
| Expressive mode | On, with the audio tags Warmly (greeting, thanks, goodbye) and Patiently (reading hours back, an unsure owner) |
| Text normalisation | `system_prompt`. The prompt and the service's date lists already spell times out ("seven thirty to one"). |
| Speech recognition | Scribe realtime, high quality, PCM 16 kHz |
| Turn-taking | Turn model v3, eagerness **eager**, speculative turn on, turn timeout 7 s, speech transcribed while interruptions are off |
| Interruption-ignore words | mmhmm, uhhuh, uh huh, uh, mm, hmm, okay, ok, okey, i see, go on, go ahead, right, gotcha |
| Soft timeout | **Off**. A "One moment." filler was tried; it also fired after tool calls and made the agent wordier. |
| First message | Interruptible |
| Max call length | 300 s |
| Privacy | Audio recorded, 30-day retention |
| Overrides | Prompt and LLM overrides **off**, because the agent is public and its tools write to listings. Per-call data comes in only through dynamic variables. |
| Authentication | Off (public agent), which lets the browser console connect by agent ID |

### Tools

| Tool | Endpoint | Body | Settings |
|---|---|---|---|
| `record_decision` | `POST /voice/tools/record-decision` | `call_id`, `conversation_id`, `item`, `decision` (APPROVE, EDIT or DECLINE), `hours` (`HH:MM-HH:MM`, comma-separated intervals, or `closed`; EDIT only), `confirmed`, `owner_comment` | Pre-tool speech **Auto**, typing sound **Always**, interruptions off while it runs, errors passed to the agent, 20 s timeout |
| `set_standing_permission` | `POST /voice/tools/standing-permission` | `call_id`, `scope` (PUBLIC_HOLIDAYS), `granted`, `confirmed` | Same as above |
| `set_contact_preference` | `POST /voice/tools/contact-preference` | `call_id`, `preference` (CALL_LATER, TEXT_ONLY or NO_MORE_CALLS), `when`, `note` | Pre-tool speech Auto, typing sound Auto, interruptions allowed |
| `end_call`, `skip_turn`, `voicemail_detection` | built in | | Voicemail detection leaves `{{voicemail_message}}` |

`call_id` is a dynamic variable: the service creates a call, passes its ID to the agent, and the tools send it back. The service rejects saves without `confirmed=true`, invalid hours, already-decided dates and unknown dates, so a prompt mistake can't write a wrong listing.

Pre-tool speech was "force" at first, which made the agent say "Saving that now, one moment." before every save. Auto plus the typing sound removed that.

### Dynamic variables (sent by the voice service per call)

| Variable | Example |
|---|---|
| `brand` | Fonecta Connect |
| `owner_first_name`, `business_name`, `city` | Max, Cinnamon Swirl Café, Helsinki |
| `today` | Tuesday 15 December 2026 |
| `text_history` | a WhatsApp message 26 hours ago, no reply yet |
| `suggestions` | Numbered list of dates: what the listing shows, the suggestion, and why |
| `call_id` | Call reference for the tools; never spoken |
| `greeting` | Rendered opening line (production first message) |
| `voicemail_message` | Left by voicemail detection |

In the dashboard's **Preview**, the test values are used, so `call_id` is `preview`. The voice service in this repository treats that ID as a demo session on the sandbox business (`_get_call_for_tool` in `voice_service/app.py`), so a Preview conversation really saves to the sandbox. That is convenient for demos. For production, remove it so only calls the service started can write. Real calls are started from the voice console or by phone.

### Post-call analysis (`analysis.json`)
- **Evaluation criteria:** `explicit_confirmation_before_saving`, `ai_disclosure_and_scope`, `fonecta_p8_clear_outcome`.
- **Data collection:** the call outcome, one line per decided date, and anything the owner asked for that needs a person at Fonecta.
- **Analysis model:** Gemini 2.5 Flash.

## How it was tuned

A benchmark in the voice service (`/eval`) had scripted owners talk to the live agent in text mode and scored each call from the tool log, not from what the agent said. It covered a normal owner, a hesitant owner and an owner who asks for something off-topic.
- **DeepSeek Flash 4.1** won: every call correct, no unsafe saves, replies in about 0.3 s (0.6 s at the slow end), about 0.9 s after a save. It is cheaper per minute than the runner-up, GLM 5.2, which was also correct but sometimes paused for 3 to 5 s.
- **Ruled out:** GPT-5.4 Nano saved a date without asking.
- **Measured in voice:** speech recognition under 60 ms, LLM 90 to 380 ms, TTS about 70 ms, tool 0.3 s.

## Things to know on the platform
- **Conversations:** every call's transcript with per-turn speech recognition, LLM, TTS and tool times. The best place to debug.
- **Tool status:** "Succeeded" on a tool call means our server answered, not that the save worked. Read the tool response.
- **Webhook tool JSON:** the dashboard format differs from the API format. Properties are a list, and the body object takes `dynamic_variable: ""` and no `constant_value`. The files in `tools/` are in the export's format.
- **Credits:** text-only test conversations cost credits too, about 115 each on our plan.
- **Phone calls:** need a phone number imported under Channels (Twilio or SIP). A Twilio trial account cannot call Finland.
