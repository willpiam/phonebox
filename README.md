phonebox

# phonebox

A local REST API that lets agents on this machine:

- make phone calls
- send and receive SMS
- send and receive email

## Start

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
3. Set an OpenAI API key at `http://127.0.0.1:8765/gui/openai` (writes gitignored `openai.json`).
4. Ensure the owned Twilio number can place Voice calls.

`POST /call` returns immediately with a call id; poll `GET /call/<id>` for status, transcript, and answers. Inbound human→agent calls are not supported yet.

AI-initiated calls may be regulated (for example TCPA in the US). Only call numbers you are allowed to contact.

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