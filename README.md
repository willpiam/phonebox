# phonebox

Local REST API for sending and reading SMS and email on owned accounts.

## ownedPhoneNumbers.json

Create `ownedPhoneNumbers.json` in this directory before sending SMS. It is gitignored because it holds Twilio secrets. (Legacy filename `owned.json` is still accepted if the new file is missing.)

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

## Start

```bash
python3 server.py
```

The server prints a link to `/explain`. Open that for what the API does and how to call it.

Listens on http://127.0.0.1:8765 by default.

Main routes:

- `GET /gui` — minimal HTML UI to edit owned phones, emails, and contacts
- `POST /send/text` — SMS
- `POST /send/email` — email
- `GET /received/<phone>` — SMS inbox
- `GET /received/email/<address>?unread=1` — email inbox (optionally unread only)
- `POST /email/read` — mark email(s) read via IMAP `\Seen`
