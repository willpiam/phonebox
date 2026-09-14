# How To Handle Phone Calls

Outbound-only for now: agents place calls; inbound human→agent calling is not supported yet.

## Stack (Option A)

- Twilio Voice + bidirectional Media Streams
- Local WebSocket bridge in phonebox (`call_media.py` on port 8766)
- Cloudflare Quick Tunnel (`cloudflared`) so Twilio can reach the local bridge without a hosted web server
- OpenAI Realtime for the on-call agent; chat completions to extract answers after the call

## Request shape

```json
{
    "to": "123 456 7891",
    "from": "098 765 4321",
    "context": {
        "files": [
            "path/from/root/to/file.txt"
        ],
        "background": "Background the caller agent should preserve.",
        "additional": "Optional extra free text."
    },
    "questions": [
        "blah blah blah?",
        "blah blah?"
    ]
}
```

## API

1. `POST /call` → HTTP 202 with `{ "id", "status": "queued", ... }`
2. Poll `GET /call/<id>` until `completed`, `failed`, or `canceled`
3. Read `transcript` and `answers`
4. Optional: `POST /call/<id>/hangup`

Also: `GET /calls`, `GET /openai` (configured flag only), `/gui/openai` for the API key.

## User setup beyond SMS

1. `pip install -r requirements.txt`
2. Install `cloudflared` on PATH
3. Configure OpenAI key via `/gui/openai`
4. Voice-enabled owned Twilio number
