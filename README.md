![logo](assets/logo_small.jpg)

# phonebox

A local tool that lets agents on this machine:

- make phone calls
- send and receive SMS
- send and receive email

Two interfaces share the same actions:

- **CLI** (`python3 cli.py …`) — one-shot commands; no always-on process. Preferred for agents.
- **REST** (`python3 server.py`) — localhost HTTP API plus HTML config UI and background inbox polling.

## CLI (no background server)

```bash
pip install -r requirements.txt
python3 cli.py explain
python3 cli.py numbers
python3 cli.py send text --from '+1XXXXXXXXXX' --to '+1YYYYYYYYYY' --body 'hello'
python3 cli.py received sms +1XXXXXXXXXX
python3 cli.py call --from '+1XXXXXXXXXX' --to '+1YYYYYYYYYY' \
  --background 'Confirm appointment' --question 'Are they available Friday?'
```

Commands print JSON on stdout (errors on stderr). Outbound calls **block** until the call finishes, then print the final record (status, transcript, answers). Ctrl+C hangs up.

| Command | Purpose |
|---------|---------|
| `explain` | Agent briefing |
| `numbers` / `addresses` / `contacts` / `openai` | List config (no secrets) |
| `contact add --name …` | Add/update a contact |
| `send text --from … --to … --body …` | SMS |
| `send email --from … --to … --subject … --body …` | Email |
| `received sms <phone> [--since …]` | SMS inbox (polls Twilio first) |
| `received email <address> [--since …] [--unread]` | Email inbox (polls IMAP first) |
| `email read --address … --message-id …` | Mark email(s) read |
| `call --from … --to … [--background …] [--question …] [--file …]` | Place call (blocking) |
| `call status <id>` / `call hangup <id>` / `calls` | Inspect or end calls |

## REST server

```bash
pip install -r requirements.txt
python3 server.py
```

The server prints a link to `/explain`. Open that for what the API does and how to call it.

Listens on [http://127.0.0.1:8765](http://127.0.0.1:8765) by default.

Main routes:

- `GET /gui` — minimal HTML UI to edit owned phones, emails, contacts, and OpenAI key
- `POST /send/text` — SMS
- `POST /send/email` — email
- `POST /call` — outbound AI phone call (async)
- `GET /call/<id>` — call status / transcript / answers
- `GET /calls` — recent calls
- `POST /call/<id>/hangup` — end a call
- `GET /received/<phone>` — SMS inbox
- `GET /received/email/<address>?unread=1` — email inbox (optionally unread only)
- `POST /email/read` — mark email(s) read via IMAP `\Seen`

## Phone calls (extra setup)

Outbound AI calls use Twilio Media Streams, a local WebSocket bridge, a Cloudflare Quick Tunnel, and OpenAI Realtime.

1. `pip install -r requirements.txt`
2. Install `[cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/)` so it is on your `PATH` (Quick Tunnels need no Cloudflare account).
3. Set an OpenAI API key at `http://127.0.0.1:8765/gui/openai` while the REST server is running (writes gitignored `openai.json`), or create `openai.json` yourself with `{"api_key":"sk-..."}`.
4. Ensure the owned Twilio number can place Voice calls.

Via CLI, `python3 cli.py call …` blocks until the call completes. Via REST, `POST /call` returns immediately with a call id; poll `GET /call/<id>` for status, transcript, and answers. Inbound human→agent calls are not supported yet.

AI-initiated calls may be regulated (for example TCPA in the US). Only call numbers you are allowed to contact.

### OpenAI API key

1. Create an account at [platform.openai.com](https://platform.openai.com/).
2. Open [API keys](https://platform.openai.com/api-keys) and create a secret key.
3. Ensure the project has billing enabled and access to the Realtime API (used for live calls).
4. Paste the key at [http://127.0.0.1:8765/gui/openai](http://127.0.0.1:8765/gui/openai) (start `python3 server.py` first), or write gitignored `openai.json` with `{"api_key":"sk-..."}`.

### Twilio phone number

1. Create an account at [twilio.com](https://www.twilio.com/) and open the [Console](https://console.twilio.com/).
2. Note your **Account SID** and **Auth Token** from the console dashboard.
3. Buy a number under [Phone Numbers → Manage → Buy a number](https://console.twilio.com/us1/develop/phone-numbers/manage/search). Enable **Voice** (and **SMS** if you want texting).
4. Open the number's detail page and copy its **Phone Number SID** (starts with `PN`).
5. Add the number to `ownedPhoneNumbers.json` (see below), or use [http://127.0.0.1:8765/gui/phones](http://127.0.0.1:8765/gui/phones).

## ownedPhoneNumbers.json

Create `ownedPhoneNumbers.json` in this directory before sending SMS or placing calls. It is gitignored because it holds Twilio secrets. (Legacy filename `owned.json` is still accepted if the new file is missing.)

```json
[
    {
        "number": "+12345678900",
        "sid": "PNxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "account_sid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "auth_token": "your_auth_token"
    }
]
```

You can add more than one number. Values come from the Twilio console:

- `number` — the Twilio phone number you send and receive with
- `sid` — that number's Phone Number SID (starts with `PN`)
- `account_sid` — Account SID (starts with `AC`)
- `auth_token` — Account auth token

For phone calls, the number must have **Voice** enabled in Twilio.

## ownedEmailAddresses.json

Create `ownedEmailAddresses.json` for email. It is gitignored because it holds mailbox passwords.

```json
[
    {
        "address": "you@example.com",
        "password": "shared-login-password",
        "display_name": "Your Name",
        "imap": {
            "host": "imap.example.com",
            "port": 993,
            "ssl": true
        },
        "smtp": {
            "host": "smtp.example.com",
            "port": 465,
            "ssl": true
        }
    }
]
```

Password rules:

- If top-level `password` is set, it is used for IMAP and SMTP.
- If top-level `password` is omitted, both `imap.password` and `smtp.password` must be set.
- Protocol-specific passwords override the top-level password when present.
