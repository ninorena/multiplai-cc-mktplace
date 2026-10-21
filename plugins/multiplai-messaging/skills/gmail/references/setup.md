# Gmail credential setup (one-time, needs the user's Google account)

The skill authenticates with an OAuth credential scoped to exactly
`gmail.compose` + `gmail.readonly`. Unlike Slack's single `xoxp-…` token, Google's
credential is a **trio** — client id, client secret, and a long-lived refresh
token — because access tokens expire hourly and are minted on the fly. Obtaining
the refresh token needs a browser + localhost redirect, so the one-time consent
runs on the **Mac host**, not in the container.

## 1. Google Cloud (once)

1. <https://console.cloud.google.com> → create a project (e.g. `gmail-drafter`) →
   **APIs & Services → enable the Gmail API**.
2. **OAuth consent screen** → User type **External** → **Testing** mode → add the
   user's Gmail address as the sole test user. (Testing needs no verification review.)
3. **Credentials → Create OAuth client → Desktop app** → download the
   client-secret JSON.

## 2. Mint the credential (on the Mac host)

Run `get_token.py` with your downloaded client-secret JSON. It requests only
compose+readonly and **prints three env vars** — nothing is written to disk:

```bash
uv run --project "<plugin>/skills/gmail/scripts" "<plugin>/skills/gmail/scripts/get_token.py" /path/to/client_secret.json
```
`<plugin>` is the installed multiplai-messaging plugin path (or the repo checkout).
For a read-only credential, add `--readonly`: it requests `gmail.readonly`
alone, so search and read work and draft does not.

Approve the scopes in the browser; it prints:

```
GMAIL_CLIENT_ID=...apps.googleusercontent.com
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=1//...
```

## 3. Add them to the kit `.env`

Paste those three lines into `$CLAUDE_MULTIPLAI_HOME/.env`. `claude.sh` forwards
them into the container exactly like `SLACK_TOKEN`. Restart the container, then
ask to draft a reply to confirm end-to-end.

## Why env vars (like Slack), not a file

The credential is a secret, so it lives in `.env` — the same place as
`SLACK_TOKEN` and `GH_TOKEN` — and is forwarded per-run, never persisted in the
workspace or in git. This replaces the old `~/.gmail-drafter/token.json` file and
its dedicated read-only container mount. (If you *prefer* a file, `get_token.py
--out <path>` writes a JSON token and `GMAIL_TOKEN_FILE` points the skill at it —
but env vars are the default.)

## Notes

- **Refresh-token expiry in Testing mode:** with the user as sole test user, some
  consent-screen configs expire the refresh token after 7 days. If drafting
  suddenly 401s about a week in, re-run `get_token.py --force`.
- **Adding capability later:** *this script* implements only search/read/draft
  and refuses any scope beyond compose+readonly. Note the `gmail.compose` token
  itself already authorizes sending at the API level — the send boundary is the
  script's absence of a send path, not the token. Broadening the script is a
  deliberate code change here; genuinely constraining the *token* (so no process
  in the container could send) is separate, future work.
- The token is the only secret. Never commit it; never paste it into chat.
