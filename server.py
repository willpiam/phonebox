#!/usr/bin/env python3
"""Local REST API for sending and reading SMS and email on owned accounts.

GET  /                          JSON API index
GET  /explain                   Plain-text briefing for another AI
GET  /gui                       Minimal HTML config UI (phones, emails, contacts, OpenAI)
GET  /numbers                   Owned phone numbers (no secrets)
GET  /addresses                 Owned email addresses (no secrets)
GET  /contacts                  Contacts from contacts.json
GET  /openai                    Whether an OpenAI API key is configured (no secret)
POST /contact/add               Add a contact. JSON: {"name", "phone"?, "email"?, "notes"?} (phone or email required)
POST /send/text                 Send SMS. JSON: {"from", "to", "body"}
POST /send/email                Send email. JSON: {"from", "to", "subject", "body", ...}
POST /email/read                Mark email(s) read on IMAP. JSON: {"address", ...}
POST /call                      Place outbound AI phone call (async). Returns call id.
GET  /call/<id>                 Call status, transcript, and answers
GET  /calls                     Recent outbound calls
POST /call/<id>/hangup          End a call
GET  /received/<phone>          Inbound SMS for an owned number
GET  /received/<phone>/since/<time>
GET  /received/email/<address>  Inbound email for an owned address
GET  /received/email/<address>/since/<time>
                                Optional query: unread=1

Also see cli.py for a one-shot CLI with the same actions (no always-on server).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import actions
import call_media
import calls
import common
import gui

POLL_INTERVAL_DEFAULT = 15
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_http_server: ThreadingHTTPServer | None = None
_stop_event: threading.Event | None = None


def request_shutdown() -> None:
    """Stop serve_forever after the current response can flush."""

    def _stop() -> None:
        time.sleep(0.15)
        if _http_server is not None:
            _http_server.shutdown()
        if _stop_event is not None:
            _stop_event.set()
        print("shutdown requested via /gui/shutdown", flush=True)

    threading.Thread(target=_stop, daemon=True).start()


def project_directory_for_explain() -> tuple[str, str | None]:
    """Return (display_path, file_uri_or_None) for the phonebox install directory.

    Uses an absolute, OS-native path derived from this package's location so agents
    know where to run ``python3 cli.py``. Falls back to a fixed message when unknown.
    """
    fallback = "could not identify local directory"
    try:
        root = common.HERE.resolve()
        if not root.is_dir():
            return fallback, None
        path_text = str(root)
        try:
            file_uri = root.as_uri()
        except ValueError:
            file_uri = None
        return path_text, file_uri
    except Exception:
        return fallback, None


def explain_text(host: str, port: int) -> str:
    base = f"http://{host}:{port}"
    project_dir, project_uri = project_directory_for_explain()
    if project_uri:
        project_dir_block = f"{project_dir}\n{project_uri}"
    else:
        project_dir_block = project_dir

    try:
        numbers = actions.list_numbers()
    except Exception as error:
        numbers = []
        number_block = f"(could not load ownedPhoneNumbers.json: {error})"
    else:
        if numbers:
            lines = []
            for item in numbers:
                e164 = item.get("e164") or "?"
                pretty = item.get("number") or e164
                lines.append(f"- {pretty}  (use {e164} in API calls)")
            number_block = "\n".join(lines)
        else:
            number_block = "(none listed in ownedPhoneNumbers.json)"

    try:
        addresses = actions.list_addresses()
    except Exception as error:
        addresses = []
        address_block = f"(could not load ownedEmailAddresses.json: {error})"
    else:
        if addresses:
            lines = []
            for item in addresses:
                addr = item.get("normalized") or item.get("address") or "?"
                display = item.get("display_name")
                if display:
                    lines.append(f"- {display} <{addr}>")
                else:
                    lines.append(f"- {addr}")
            address_block = "\n".join(lines)
        else:
            address_block = "(none listed in ownedEmailAddresses.json)"

    example_from = (numbers[0].get("e164") if numbers else "+1XXXXXXXXXX") or "+1XXXXXXXXXX"
    example_email = (
        (addresses[0].get("normalized") if addresses else "you@example.com")
        or "you@example.com"
    )
    return f"""You have a local messaging tool called phonebox.

What it is
Phonebox lets programs and AIs on this machine send and read real SMS (Twilio) and real email (IMAP/SMTP), and place outbound AI phone calls (Twilio Voice + OpenAI Realtime). It is not a simulator.

Two interfaces (same actions)
1. CLI (preferred when you do not need a long-running process): run commands via python3 cli.py … — each command exits when done. Outbound calls block until the call finishes, then print the final JSON record. No always-on server required for SMS, email, or a single outbound call.
2. REST API (this server): HTTP on localhost. Needed for the HTML config UI (/gui) and continuous background inbox polling. POST /call is async; poll GET /call/<id>.

CLI working directory (run commands from here)
{project_dir_block}
Change into that directory first (or pass an absolute path to cli.py). Config files such as ownedPhoneNumbers.json and openai.json live in the same directory.

CLI quick start
python3 cli.py explain
python3 cli.py numbers
python3 cli.py send text --from '{example_from}' --to '+15195551212' --body 'hello'
python3 cli.py received sms {example_from}
python3 cli.py call --from '{example_from}' --to '+15195551212' --background 'Confirm appointment' --question 'Are they available Friday?'

REST base URL
{base}
Bound to localhost only. Do not assume it is reachable from the internet.

Human config UI
GET {base}/gui
Minimal HTML pages to add/edit/delete owned phones, owned emails, contacts, and the OpenAI API key. Prefer this for humans; prefer the CLI or JSON API below for programs and AIs.

Owned phone numbers
GET {base}/numbers
CLI: python3 cli.py numbers
Use one of these as "from" when sending SMS or placing calls, and as <phone> when reading the SMS inbox. Current owned numbers:

{number_block}

Owned email addresses
GET {base}/addresses
CLI: python3 cli.py addresses
Use one of these as "from" when sending email, and as <address> when reading an email inbox. Current owned addresses:

{address_block}

OpenAI (required for phone calls)
GET {base}/openai
CLI: python3 cli.py openai
Returns {{"configured": true/false, "realtime_model": "..."}}. Never returns the API key. Configure the key at GET {base}/gui/openai (writes gitignored openai.json).

Contacts
GET {base}/contacts
CLI: python3 cli.py contacts
Returns saved people (name, optional phone/e164, optional email, optional notes). Each contact has at least a phone or an email. Use a contact's phone as "to" when sending SMS or placing a call. Use a contact's email address as "to" when sending email — do not pass the contact name to /send/email.

POST {base}/contact/add
CLI: python3 cli.py contact add --name 'Ada Lovelace' --phone '519 555 1212' --email 'ada@example.com'
Content-Type: application/json

{{
  "name": "Ada Lovelace",
  "phone": "519 555 1212",
  "email": "ada@example.com",
  "notes": "met at conference"
}}

"number" is accepted as an alias for "phone". Phone and email are both optional, but at least one is required. "notes" is optional free text. Success is HTTP 201 for a new contact, or 200 if that phone or email was already saved (fields are updated).

Send SMS
POST {base}/send/text
CLI: python3 cli.py send text --from '{example_from}' --to '+15195551212' --body 'your message'
Content-Type: application/json

{{
  "from": "{example_from}",
  "to": "+15195551212",
  "body": "your message"
}}

Rules:
- "from" must be an owned number (or its SID). Formats like (236) 243-8512, 2362438512, or +12362438512 all work.
- "to" is the recipient phone number. Same format flexibility. "recipient" is accepted as an alias for "to".
- "body" is the message text. "content" is accepted as an alias for "body". Must be non-empty.
- Bodies longer than Twilio's 1600-character limit are split into multiple SMS on word boundaries (whitespace), each prefixed "(1/n)", "(2/n)", … so the reader can reorder if parts arrive out of sequence. A single word longer than the per-part budget is hard-split. Parts are sent back-to-back; SMS does not guarantee delivery order.
- Success is HTTP 201 with the stored sent record (includes provider_sid, status, created_at_unix). If the body was split, the response is {{"count": N, "messages": [record, ...]}} instead of a single record.
- Failures are JSON {{"error": "..."}} with 400 for bad input or 500 if Twilio rejects the send.

Place outbound phone call
REST (async): POST {base}/call then poll GET {base}/call/<id>
CLI (blocking): python3 cli.py call --from '{example_from}' --to '+15195551212' --background '...' --question '...'
Content-Type: application/json

{{
  "from": "{example_from}",
  "to": "+15195551212",
  "context": {{
    "files": ["notes/example.txt"],
    "background": "Why you are calling and what the callee should know.",
    "additional": "Optional extra free text."
  }},
  "questions": [
    "What is their preferred callback time?",
    "Did they confirm the appointment?"
  ]
}}

Rules:
- Outbound only for now (no inbound human→agent calls).
- "from" must be a Voice-capable owned Twilio number. "to" is the callee.
- "context.files" are paths under the phonebox project directory (path escape outside the project is rejected).
- REST returns HTTP 202 immediately with a call record including "id" and status "queued". Poll GET {base}/call/<id> until status is completed, failed, or canceled.
- CLI blocks until the call reaches a terminal status, then prints the final JSON record (transcript, answers).
- Statuses: queued → tunneling → dialing → in_progress → completed | failed | canceled.
- On completion, "transcript" holds turn text and "answers" maps each question to an extracted answer.
- POST {base}/call/<id>/hangup or python3 cli.py call hangup <id> ends an in-progress call.
- GET {base}/calls or python3 cli.py calls lists recent calls (newest first).
- Requires: OpenAI key configured, cloudflared on PATH, Voice enabled on the Twilio number.
- phonebox starts a Cloudflare Quick Tunnel for the call media WebSocket; you do not host a public server.

Send email
POST {base}/send/email
CLI: python3 cli.py send email --from '{example_email}' --to 'recipient@example.com' --subject 'hello' --body 'hi'
Content-Type: application/json

{{
  "from": "{example_email}",
  "to": ["recipient@example.com"],
  "cc": [],
  "bcc": [],
  "subject": "hello",
  "body": "your message",
  "attachments": []
}}

Rules:
- "from" must be an owned email address from /addresses.
- "to" is required (string or array of email addresses). Pass real addresses, not contact names. Look up a contact's email via GET /contacts first if needed. "cc", "bcc", and "attachments" are optional.
- "subject" is required. "body"/"content" is the plain-text body (may be empty string).
- Success is HTTP 201 with the stored sent email record (message_id / provider_sid, created_at_unix).
- Failures are JSON {{"error": "..."}} with 400 for bad input or 500 if SMTP rejects the send.

Read inbound SMS
GET {base}/received/<phone>
CLI: python3 cli.py received sms <phone> [--since <time>]
<phone> is the owned number whose inbox you want (the number texts were sent TO).

Optional cutoff:
GET {base}/received/<phone>/since/<time>
<time> is a unix timestamp (seconds) or an ISO-8601 datetime. Returns messages with created_at_unix strictly greater than that cutoff.

The SMS inbox is refreshed from Twilio when you call /received or the CLI received command, and also in the background about every 15 seconds while the REST server is running.

Read inbound email
GET {base}/received/email/<address>
CLI: python3 cli.py received email <address> [--since <time>] [--unread]
<address> is the owned email whose inbox you want.

Optional cutoff:
GET {base}/received/email/<address>/since/<time>

Unread only (important for multi-agent handoff):
GET {base}/received/email/<address>?unread=1
GET {base}/received/email/<address>/since/<time>?unread=1
CLI: python3 cli.py received email <address> --unread

Unread means the message does not have IMAP \\Seen yet (and local history read=false). After you handle an email, mark it read so another AI instance will not process it again.

Mark email(s) read
POST {base}/email/read
CLI: python3 cli.py email read --address '{example_email}' --message-id '<abc@example.com>'
Content-Type: application/json

{{
  "address": "{example_email}",
  "message_ids": ["<abc@example.com>"]
}}

Also accepts "uids" (IMAP UIDs as strings). At least one of message_ids or uids is required.

Example mark-read response
{{
  "address": "{example_email}",
  "marked": 1,
  "message_ids": ["<abc@example.com>"]
}}

Example inbound email list response (shape)
{{
  "address": "{example_email}",
  "count": 1,
  "unread_only": false,
  "messages": [
    {{
      "kind": "email",
      "direction": "inbound",
      "from": "sender@example.com",
      "to": ["{example_email}"],
      "subject": "hello",
      "body": "plain text body",
      "has_html": false,
      "message_id": "<abc@example.com>",
      "imap_uid": "42",
      "read": false,
      "created_at_unix": 1788979464,
      "created_at_iso": "2026-09-09T14:44:24-04:00"
    }}
  ]
}}
Messages are oldest-first. "since" is omitted unless you used /since/<time>.
Plain text is in "body". Large HTML is omitted; if HTML exists alongside plain text you get "has_html": true. If there is little/no plain text, "html" is included.

History files (for humans; prefer the CLI or API)
- sent.history.json / received.history.json — SMS
- sent.email.history.json / received.email.history.json — email
- calls.history.json — outbound phone calls
Do not read auth tokens or passwords from ownedPhoneNumbers.json, ownedEmailAddresses.json, or openai.json. The API never returns credentials.

Typical AI workflow (SMS)
1. python3 cli.py numbers (or GET /numbers) and pick an owned "from" number.
2. python3 cli.py contacts if you need a recipient by name.
3. python3 cli.py send text --from … --to … --body …
4. Note created_at_unix (or time.now) as a cursor.
5. Later python3 cli.py received sms <from> --since <cursor> to see replies.

Typical AI workflow (email)
1. python3 cli.py addresses and pick an owned "from" address.
2. python3 cli.py contacts if you need a recipient email by name (then use that email address, not the name).
3. python3 cli.py received email <address> --unread and handle those messages.
4. python3 cli.py email read --address … --message-id …
5. python3 cli.py send email when a reply is needed.

Typical AI workflow (phone call)
1. python3 cli.py openai and confirm configured=true (else tell the human to open /gui/openai).
2. python3 cli.py numbers and pick a Voice-capable owned "from" number.
3. python3 cli.py call --from … --to … --background … --question … (blocks until done), or POST /call then poll GET /call/<id>.
4. Read answers and transcript from the final record.

Examples (curl / REST)
curl -s {base}/numbers
curl -s {base}/addresses
curl -s {base}/openai
curl -s {base}/contacts
curl -s -X POST {base}/contact/add -H 'Content-Type: application/json' \\
  -d '{{"name":"Ada Lovelace","phone":"519 555 1212","email":"ada@example.com"}}'
curl -s -X POST {base}/send/text -H 'Content-Type: application/json' \\
  -d '{{"from":"{example_from}","to":"+15195551212","body":"hello"}}'
curl -s -X POST {base}/send/email -H 'Content-Type: application/json' \\
  -d '{{"from":"{example_email}","to":["recipient@example.com"],"subject":"hello","body":"hi"}}'
curl -s -X POST {base}/call -H 'Content-Type: application/json' \\
  -d '{{"from":"{example_from}","to":"+15195551212","context":{{"background":"Confirm appointment"}},"questions":["Are they available Friday?"]}}'
curl -s {base}/call/CALL_ID
curl -s {base}/calls
curl -s {base}/received/{example_from}
curl -s '{base}/received/email/{example_email}?unread=1'
curl -s -X POST {base}/email/read -H 'Content-Type: application/json' \\
  -d '{{"address":"{example_email}","message_ids":["<abc@example.com>"]}}'

Constraints
- Only send SMS or place calls from numbers returned by /numbers or cli.py numbers.
- Only send email from addresses returned by /addresses or cli.py addresses.
- This sends real SMS, email, and phone calls. Do not spam. Confirm recipients before contacting them. AI phone calls may be regulated (e.g. TCPA); obtain consent where required.
- Phone numbers in URLs may include +. Prefer the E.164 form (+1...) or digits-only.
- After handling an email, mark it read so other agents skip it.
"""


def api_index() -> dict:
    return {
        "service": "phonebox",
        "listen": "localhost",
        "cli": "python3 cli.py — one-shot CLI with the same actions (no always-on server)",
        "endpoints": [
            {
                "method": "GET",
                "path": "/explain",
                "description": "Plain-text briefing: what this tool is and how to use it",
            },
            {
                "method": "GET",
                "path": "/gui",
                "description": "Minimal HTML UI to edit owned phones, emails, contacts, and OpenAI key",
            },
            {
                "method": "GET",
                "path": "/openai",
                "description": "Whether an OpenAI API key is configured (never returns the key)",
            },
            {
                "method": "GET",
                "path": "/numbers",
                "description": "List owned phone numbers available for sending and receiving SMS",
            },
            {
                "method": "GET",
                "path": "/addresses",
                "description": "List owned email addresses available for sending and receiving email",
            },
            {
                "method": "GET",
                "path": "/contacts",
                "description": "List saved contacts",
            },
            {
                "method": "POST",
                "path": "/contact/add",
                "description": "Add a contact",
                "body": {
                    "name": "display name",
                    "phone": "optional phone",
                    "email": "optional email",
                    "notes": "optional notes",
                },
            },
            {
                "method": "POST",
                "path": "/send/text",
                "description": "Send an SMS",
                "body": {"from": "owned number", "to": "recipient number", "body": "message text"},
            },
            {
                "method": "POST",
                "path": "/send/email",
                "description": "Send an email",
                "body": {
                    "from": "owned email",
                    "to": ["recipient@example.com"],
                    "subject": "subject",
                    "body": "plain text",
                },
            },
            {
                "method": "POST",
                "path": "/email/read",
                "description": "Mark one or more emails as read (IMAP \\Seen)",
                "body": {
                    "address": "owned email",
                    "message_ids": ["<id@example.com>"],
                    "uids": ["42"],
                },
            },
            {
                "method": "GET",
                "path": "/received/<phone>",
                "description": "Inbound SMS received by an owned number",
            },
            {
                "method": "GET",
                "path": "/received/<phone>/since/<time>",
                "description": "Inbound SMS after a unix timestamp or ISO-8601 time",
            },
            {
                "method": "GET",
                "path": "/received/email/<address>",
                "description": "Inbound email for an owned address; optional ?unread=1",
            },
            {
                "method": "GET",
                "path": "/received/email/<address>/since/<time>",
                "description": "Inbound email after a cutoff; optional ?unread=1",
            },
            {
                "method": "POST",
                "path": "/call",
                "description": "Place an outbound AI phone call (async; poll GET /call/<id>)",
                "body": {
                    "from": "owned number",
                    "to": "recipient number",
                    "context": {
                        "files": ["path/under/phonebox"],
                        "background": "free text",
                        "additional": "free text",
                    },
                    "questions": ["what to learn on the call"],
                },
            },
            {
                "method": "GET",
                "path": "/call/<id>",
                "description": "Call status, transcript, and extracted answers",
            },
            {
                "method": "GET",
                "path": "/calls",
                "description": "Recent outbound calls (newest first)",
            },
            {
                "method": "POST",
                "path": "/call/<id>/hangup",
                "description": "Hang up an in-progress outbound call",
            },
        ],
    }


def parse_unread_flag(query: str) -> bool:
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    values = params.get("unread") or params.get("unread_only") or []
    if not values:
        return False
    value = (values[0] or "1").strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} {format % args}", flush=True)

    def _write(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: int, payload: str) -> None:
        body = payload.encode("utf-8")
        self._write(status, "text/html; charset=utf-8", body)

    def _redirect(self, location: str) -> None:
        body = f'<a href="{gui.escape(location)}">redirect</a>\n'.encode("utf-8")
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
        self._write(status, "application/json; charset=utf-8", body)

    def _text(self, status: int, payload: str) -> None:
        body = payload.encode("utf-8")
        if not body.endswith(b"\n"):
            body += b"\n"
        self._write(status, "text/plain; charset=utf-8", body)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path).rstrip("/") or "/"
        try:
            if path in {"/", "/help"}:
                self._json(200, api_index())
                return
            if path == "/explain":
                host, port = self.server.server_address[:2]
                self._text(200, explain_text(str(host), int(port)))
                return
            if path == "/assets" or path.startswith("/assets/"):
                self._handle_asset(path)
                return
            if path in gui.ROUTES_GET or path.startswith("/gui"):
                self._handle_gui_get(path, parsed.query)
                return
            if path == "/numbers":
                self._json(200, {"numbers": actions.list_numbers()})
                return
            if path == "/addresses":
                self._json(200, {"addresses": actions.list_addresses()})
                return
            if path == "/contacts":
                self._json(200, {"contacts": actions.list_contacts_public()})
                return
            if path == "/openai":
                self._json(200, actions.openai_status())
                return
            if path == "/calls":
                items = calls.list_calls()
                self._json(200, {"calls": items, "count": len(items)})
                return
            if path == "/call" or path.startswith("/call/"):
                self._handle_call_get(path)
                return
            if path == "/received" or path.startswith("/received/"):
                self._handle_received(path, parsed.query)
                return
            self._error(404, f"unknown path: {path}")
        except ValueError as error:
            self._error(400, str(error))
        except Exception:
            traceback.print_exc()
            self._error(500, "internal error")

    def _handle_call_get(self, path: str) -> None:
        if path == "/call":
            self._error(400, "call id required: GET /call/<id> (or use GET /calls)")
            return
        call_id = path[len("/call/") :].strip("/")
        if not call_id or "/" in call_id:
            self._error(400, "call id required: GET /call/<id>")
            return
        record = calls.get_call(call_id)
        if record is None:
            self._error(404, f"unknown call id: {call_id}")
            return
        self._json(200, record)

    def _handle_gui_get(self, path: str, query: str) -> None:
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        message = (params.get("msg") or [None])[0]
        error = (params.get("error") or [None])[0]
        renderer = gui.ROUTES_GET.get(path)
        if renderer is None:
            self._error(404, f"unknown path: {path}")
            return
        self._html(200, renderer(message=message or None, error=error or None))

    def _handle_asset(self, path: str) -> None:
        rest = path[len("/assets") :].lstrip("/")
        if not rest or "/" in rest or rest in {".", ".."}:
            self._error(404, "asset not found")
            return
        assets_root = common.ASSETS_PATH.resolve()
        candidate = (assets_root / rest).resolve()
        try:
            candidate.relative_to(assets_root)
        except ValueError:
            self._error(404, "asset not found")
            return
        if not candidate.is_file():
            self._error(404, "asset not found")
            return
        if candidate.suffix.lower() not in gui.IMAGE_EXTENSIONS:
            self._error(404, "asset not found")
            return
        data = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._write(200, content_type, data)

    def _read_json_object(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._error(400, "body must be JSON")
            return None
        if not isinstance(payload, dict):
            self._error(400, "JSON body must be an object")
            return None
        return payload

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path).rstrip("/") or "/"
        try:
            if path == "/gui/shutdown":
                request_shutdown()
                self._html(200, gui.shutdown_page())
                return
            if path in gui.ROUTES_POST:
                self._handle_gui_post(path)
                return
            if path == "/contact/add":
                payload = self._read_json_object()
                if payload is None:
                    return
                contact, created = actions.add_contact(payload)
                self._json(201 if created else 200, contact)
                return
            if path == "/email/read":
                payload = self._read_json_object()
                if payload is None:
                    return
                result = actions.mark_emails_read(payload)
                self._json(200, result)
                return
            if path == "/send/text":
                payload = self._read_json_object()
                if payload is None:
                    return
                record = actions.send_sms(payload)
                self._json(201, record)
                return
            if path == "/send/email":
                payload = self._read_json_object()
                if payload is None:
                    return
                record = actions.send_email(payload)
                self._json(201, record)
                return
            if path == "/call":
                payload = self._read_json_object()
                if payload is None:
                    return
                record = calls.create_call_record(payload)
                self._json(202, record)
                return
            if path.startswith("/call/") and path.endswith("/hangup"):
                call_id = path[len("/call/") : -len("/hangup")].strip("/")
                if not call_id or "/" in call_id:
                    self._error(400, "call id required: POST /call/<id>/hangup")
                    return
                record = calls.hangup_call(call_id)
                self._json(200, record)
                return
            if path == "/send":
                self._error(
                    404,
                    "use POST /send/text for SMS or POST /send/email for email",
                )
                return
            self._error(404, f"unknown path: {path}")
        except ValueError as error:
            self._error(400, str(error))
        except Exception as error:
            traceback.print_exc()
            self._error(500, str(error))

    def _handle_gui_post(self, path: str) -> None:
        redirect_to, handler = gui.ROUTES_POST[path]
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            form = gui.parse_form_body(raw, self.headers.get("Content-Type"))
            message = handler(form)
            query = urllib.parse.urlencode({"msg": message})
            self._redirect(f"{redirect_to}?{query}")
        except ValueError as error:
            query = urllib.parse.urlencode({"error": str(error)})
            self._redirect(f"{redirect_to}?{query}")
        except Exception as error:
            traceback.print_exc()
            query = urllib.parse.urlencode({"error": str(error)})
            self._redirect(f"{redirect_to}?{query}")

    def _handle_received(self, path: str, query: str) -> None:
        rest = path[len("/received") :].lstrip("/")
        if not rest:
            self._error(
                400,
                "path required: /received/<phone> or /received/email/<address>",
            )
            return

        if rest == "email" or rest.startswith("email/"):
            self._handle_received_email(rest[len("email") :].lstrip("/"), query)
            return

        if "/since/" in rest:
            phone_part, time_part = rest.split("/since/", 1)
        else:
            phone_part, time_part = rest, None
        if not phone_part.strip():
            self._error(400, "phone number required: /received/<phone>")
            return
        owned = common.resolve_owned(common.load_owned(), phone_part)
        phone = common.to_e164(owned["number"])
        cutoff = common.parse_cutoff(time_part) if time_part is not None else None
        try:
            actions.poll_inbound_sms()
        except Exception as error:
            print(f"poll before /received failed: {error}", flush=True)
        messages = actions.received_for(phone, cutoff)
        payload = {"phone": phone, "count": len(messages), "messages": messages}
        if cutoff is not None:
            payload["since"] = cutoff
        self._json(200, payload)

    def _handle_received_email(self, rest: str, query: str) -> None:
        if not rest:
            self._error(400, "email address required: /received/email/<address>")
            return
        if "/since/" in rest:
            address_part, time_part = rest.split("/since/", 1)
        else:
            address_part, time_part = rest, None
        if not address_part.strip():
            self._error(400, "email address required: /received/email/<address>")
            return
        owned = common.resolve_owned_email(common.load_owned_emails(), address_part)
        address = common.normalize_email(str(owned["address"]))
        cutoff = common.parse_cutoff(time_part) if time_part is not None else None
        unread_only = parse_unread_flag(query)
        try:
            actions.poll_inbound_email()
        except Exception as error:
            print(f"poll before /received/email failed: {error}", flush=True)
        messages = actions.received_email_for(address, cutoff, unread_only=unread_only)
        payload = {
            "address": address,
            "count": len(messages),
            "unread_only": unread_only,
            "messages": messages,
        }
        if cutoff is not None:
            payload["since"] = cutoff
        self._json(200, payload)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local SMS, email, and outbound AI phone-call REST API"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port (default: 8765)")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=POLL_INTERVAL_DEFAULT,
        help="seconds between inbound polls (default: 15)",
    )
    args = parser.parse_args()

    actions.ensure_history_files()

    stop = threading.Event()
    poller = threading.Thread(
        target=actions.poll_loop, args=(args.poll_interval, stop), daemon=True
    )
    poller.start()

    global _http_server, _stop_event
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    _http_server = server
    _stop_event = stop
    print(f"phonebox listening on http://{args.host}:{args.port}", flush=True)
    print(f"http://{args.host}:{args.port}/explain", flush=True)
    print(f"http://{args.host}:{args.port}/gui", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        stop.set()
        try:
            call_media.stop_media_server()
        except Exception:
            pass
        _http_server = None
        _stop_event = None
        server.server_close()
        print("phonebox stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
