# Owner-flow verification

Verified on 8 November 2026 against the real Yext sandbox using the one-to-one
FI-D0001 mapping. No production listing or WhatsApp delivery was involved.

The rehearsal started the actual Python owner HTTP handler with the real sandbox
as its primary destination, submitted its forms, and independently read the
entire hours object back from Yext after each step. It used no Yext network double.

| Check | Result |
|---|---|
| Decline | Every saved hour remained unchanged. |
| Approve | Proposed hours were written and read back; unrelated dates and weekly hours were preserved. |
| Duplicate approval | No second Apply record was created. |
| Undo approval | The complete original hours object was restored. |
| Edit | A review draft appeared; no real hours changed before confirmation. |
| Cancel draft | No real hours changed. |
| Confirm edit | Only the named date changed; the entire read-back matched the expected hours object. |
| Undo edit | The complete original hours object was restored. |

The original snapshot, individual Apply/Undo records, and machine-readable report
are retained locally in the ignored rehearsal runtime. Secrets are not included
in this document or in the repository. The repeatable entry point is
`python app/rehearse_sandbox.py --run`; without `--run` it prints help and
does not start the backend or change hours. Do not run concurrent owner mutations
against the same sandbox entity during a rehearsal.

Browser verification additionally covered desktop and phone layouts, example
shortcuts, keyboard submission, missing-AI guidance, and approve/reset/reopen:
dates already saved no longer generate a fresh proposal.

The integrated main suites passed 157 checks (74 Apply/sandbox, 83 owner/messaging/AI/live-owner).
They include mock-provider contract and validation checks; these do not prove
live AI inference or delivery to a phone.

## Still requires the evaluator's service setup

- **OpenShift inference:** provide `OPENSHIFT_AI_URL`, `OPENSHIFT_AI_TOKEN`, and
  the deployed `OPENSHIFT_AI_MODEL` name (default `granite-hours`). The service
  must support the vLLM chat-completions JSON-schema contract. No live model
  response was verified in this rehearsal because inference credentials were
  absent. Exact date/time parsing was used for its edit step.
- **Real WhatsApp delivery:** provide the Twilio sender/template configuration,
  opted-in recipient registry and public webhook origin. No real message was sent.

Run `python app/app.py --check-services` after configuring OpenShift.
It makes a real synthetic parsing request and verifies sandbox read access,
without changing hours or sending a message. A successful live inference check
is required before claiming the natural-language path was live-tested.
