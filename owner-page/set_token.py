#!/usr/bin/env python3
"""Save the OpenShift AI model token into the repository's .env without showing it on screen.

    pbpaste | python3 owner-page/set_token.py     # take the token from the clipboard (macOS)
    python3 owner-page/set_token.py               # or paste it when asked (hidden)

Nothing is printed except the length, and .env is gitignored.
"""
import getpass
import os
import sys

ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")

if sys.stdin.isatty():
    token = getpass.getpass("Paste the OpenShift AI token and press Enter (it stays hidden): ")
else:
    token = sys.stdin.read()
token = token.strip()
if not (token.startswith("eyJ") and token.count(".") == 2 and len(token) > 300 and " " not in token):
    raise SystemExit("Your clipboard doesn't hold the token (a token starts with 'eyJ' and is very long). "
                     "Copy the token first, then run this again. Nothing changed.")

lines = open(ENV, encoding="utf-8").read().splitlines() if os.path.exists(ENV) else []
lines = [l for l in lines if not l.startswith("OPENSHIFT_AI_TOKEN=")] + [f"OPENSHIFT_AI_TOKEN={token}"]
with open(ENV, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print(f"Saved. The token is {len(token)} characters long and stored only in .env.")
