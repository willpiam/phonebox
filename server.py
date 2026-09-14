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
POST /call/<id>/hangup          End an in-progress call
GET  /received/<phone>          Inbound SMS for an owned number
GET  /received/<phone>/since/<time>
GET  /received/email/<address>  Inbound email for an owned address
GET  /received/email/<address>/since/<time>
                                Optional query: unread=1
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import call_media
import calls
import common
import gui

POLL_INTERVAL_DEFAULT = 15
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_history_lock = threading.Lock()
_poll_error: str | None = None
gui.history_lock = _history_lock
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


def send_twilio_sms(owned: dict, to_e164_number: str, body: str) -> dict:
    from_number = owned.get("number")
    account_sid = owned.get("account_sid")
    if not from_number or not account_sid:
        raise ValueError("ownedPhoneNumbers.json entry needs number and account_sid")
    url = common.TWILIO_MESSAGES.format(account_sid=account_sid)
    payload = urllib.parse.urlencode(
        {
            "From": common.to_e164(from_number),
            "To": to_e164_number,
            "Body": body,
        }
    ).encode("utf-8")
    return common.twilio_request(owned, url, data=payload)


def inbound_direction(value: str | None) -> bool:
    return (value or "").lower().startswith("inbound")


def fetch_messages_to(owned: dict, to_number: str) -> list[dict]:
    query = urllib.parse.urlencode({"To": to_number, "PageSize": "50"})
    url = common.TWILIO_MESSAGES.format(account_sid=owned["account_sid"]) + "?" + query
    messages: list[dict] = []
    while url:
        payload = common.twilio_request(owned, url)
        messages.extend(payload.get("messages") or [])
        next_page = payload.get("next_page_uri")
        url = next_page if isinstance(next_page, str) and next_page else None
    return messages


def record_from_twilio(message: dict, owned: dict) -> dict | None:
    if not inbound_direction(message.get("direction")):
        return None
    sid = message.get("sid")
    if not isinstance(sid, str) or not sid.strip():
        return None
    from_number = message.get("from")
    to_number = message.get("to")
    if not isinstance(from_number, str) or not isinstance(to_number, str):
        return None
    sent_at = common.parse_twilio_date(message.get("date_sent") or message.get("date_created"))
    num_media = message.get("num_media")
    try:
        media_count = int(num_media) if num_media is not None else 0
    except (TypeError, ValueError):
        media_count = 0
    record = {
        "kind": "sms",
        "direction": "inbound",
        "from": common.to_e164(from_number),
        "to": common.to_e164(to_number),
        "to_sid": owned.get("sid"),
        "body": message.get("body") or "",
        "status": message.get("status"),
        "provider": "twilio",
        "provider_sid": sid.strip(),
        "created_at_unix": int(sent_at.timestamp()),
        "created_at_iso": sent_at.isoformat(),
    }
    if media_count:
        record["num_media"] = media_count
    return record


def poll_inbound_sms() -> int:
    """Fetch new inbound SMS from Twilio into received.history.json. Returns new count."""
    global _poll_error
    owned_list = common.load_owned()
    fetched: list[dict] = []
    for owned in owned_list:
        number = owned.get("number")
        if not number:
            raise ValueError("ownedPhoneNumbers.json entry needs a number")
        to_number = common.to_e164(number)
        for message in fetch_messages_to(owned, to_number):
            record = record_from_twilio(message, owned)
            if record is not None:
                fetched.append(record)

    with _history_lock:
        history = common.load_list(common.RECEIVED_PATH)
        known = common.history_sids(history)
        new_records: list[dict] = []
        for item in fetched:
            sid = item["provider_sid"]
            if sid in known:
                continue
            known.add(sid)
            new_records.append(item)
        new_records.sort(key=lambda item: item.get("created_at_unix") or 0)
        if new_records:
            history.extend(new_records)
            common.save_json(common.RECEIVED_PATH, history)
    _poll_error = None
    return len(new_records)


def _merge_email_record(existing: dict, incoming: dict) -> None:
    """Refresh mutable IMAP fields on an already-known message."""
    existing["read"] = bool(incoming.get("read"))
    if incoming.get("imap_uid"):
        existing["imap_uid"] = incoming["imap_uid"]
    if incoming.get("mailbox"):
        existing["mailbox"] = incoming["mailbox"]
    # Prefer Message-ID based provider_sid when we learn one later.
    incoming_mid = incoming.get("message_id")
    if isinstance(incoming_mid, str) and incoming_mid.strip():
        existing["message_id"] = incoming_mid.strip()
        existing["provider_sid"] = incoming_mid.strip()


def poll_inbound_email() -> int:
    """Fetch IMAP mail into received.email.history.json. Returns newly added count."""
    owned_list = common.load_owned_emails()
    with _history_lock:
        history = common.load_list(common.RECEIVED_EMAIL_PATH)
        known_uids_by_address: dict[str, set[str]] = {}
        for item in history:
            address = str(item.get("to", "")).strip()
            uid = item.get("imap_uid")
            if not address or not isinstance(uid, str) or not uid.strip():
                continue
            try:
                key = common.normalize_email(address)
            except ValueError:
                continue
            known_uids_by_address.setdefault(key, set()).add(uid.strip())

    fetched: list[dict] = []
    for owned in owned_list:
        address = common.normalize_email(str(owned["address"]))
        fetched.extend(
            common.fetch_imap_messages(
                owned,
                known_uids=known_uids_by_address.get(address, set()),
            )
        )

    added = 0
    with _history_lock:
        history = common.load_list(common.RECEIVED_EMAIL_PATH)
        by_sid = {
            str(item.get("provider_sid")).strip(): item
            for item in history
            if isinstance(item.get("provider_sid"), str) and item.get("provider_sid").strip()
        }
        by_uid: dict[str, dict] = {}
        for item in history:
            address = str(item.get("to", "")).strip()
            uid = item.get("imap_uid")
            if address and isinstance(uid, str) and uid.strip():
                by_uid[f"{common.normalize_email(address)}:{uid.strip()}"] = item

        new_records: list[dict] = []
        changed = False
        for item in fetched:
            sid = str(item.get("provider_sid") or "").strip()
            address = str(item.get("to") or "")
            uid = str(item.get("imap_uid") or "").strip()
            uid_key = f"{common.normalize_email(address)}:{uid}" if address and uid else ""
            # Flag-only refresh rows use a synthetic uid: sid; prefer uid match.
            existing = by_uid.get(uid_key) if uid_key else None
            if existing is None and sid and not sid.startswith("uid:"):
                existing = by_sid.get(sid)
            if existing is not None:
                before = existing.get("read")
                _merge_email_record(existing, item)
                if existing.get("read") != before:
                    changed = True
                continue
            # Skip incomplete flag-only stubs that somehow weren't matched.
            if "subject" not in item and "body" not in item:
                continue
            new_records.append(item)
            if sid:
                by_sid[sid] = item
            if uid_key:
                by_uid[uid_key] = item

        if new_records:
            new_records.sort(key=lambda item: item.get("created_at_unix") or 0)
            history.extend(new_records)
            added = len(new_records)
            changed = True
        if changed:
            common.save_json(common.RECEIVED_EMAIL_PATH, history)
    return added


def poll_inbound() -> tuple[int, int]:
    sms_added = poll_inbound_sms()
    email_added = poll_inbound_email()
    return sms_added, email_added


def poll_loop(interval: float, stop: threading.Event) -> None:
    global _poll_error
    while not stop.is_set():
        try:
            sms_added, email_added = poll_inbound()
            parts = []
            if sms_added:
                parts.append(f"{sms_added} sms")
            if email_added:
                parts.append(f"{email_added} email")
            if parts:
                print(f"polled inbound: {', '.join(parts)} new", flush=True)
            _poll_error = None
        except Exception as error:
            _poll_error = str(error)
            print(f"poll error: {error}", flush=True)
        stop.wait(interval)


def append_sent(record: dict) -> None:
    with _history_lock:
        history = common.load_list(common.SENT_PATH)
        history.append(record)
        common.save_json(common.SENT_PATH, history)


def append_sent_email(record: dict) -> None:
    with _history_lock:
        history = common.load_list(common.SENT_EMAIL_PATH)
        history.append(record)
        common.save_json(common.SENT_EMAIL_PATH, history)


def received_for(phone: str, cutoff: float | None = None) -> list[dict]:
    with _history_lock:
        history = common.load_list(common.RECEIVED_PATH)
    matched = [item for item in history if common.numbers_match(str(item.get("to", "")), phone)]
    if cutoff is not None:
        matched = [
            item
            for item in matched
            if (item.get("created_at_unix") or 0) > cutoff
        ]
    matched.sort(key=lambda item: item.get("created_at_unix") or 0)
    return matched


def received_email_for(
    address: str,
    cutoff: float | None = None,
    *,
    unread_only: bool = False,
) -> list[dict]:
    with _history_lock:
        history = common.load_list(common.RECEIVED_EMAIL_PATH)
    matched = [
        item
        for item in history
        if common.emails_match(str(item.get("to", "")), address)
    ]
    if unread_only:
        matched = [item for item in matched if not item.get("read")]
    if cutoff is not None:
        matched = [
            item
            for item in matched
            if (item.get("created_at_unix") or 0) > cutoff
        ]
    matched.sort(key=lambda item: item.get("created_at_unix") or 0)
    return matched


def load_contacts() -> list[dict]:
    with _history_lock:
        return common.load_list(common.CONTACTS_PATH)


def public_contact(entry: dict) -> dict:
    name = str(entry.get("name", "")).strip()
    phone = str(entry.get("phone") or entry.get("number") or "").strip()
    e164 = entry.get("e164")
    if phone and not e164:
        e164 = common.to_e164(phone)
    elif not phone:
        e164 = None
    email_raw = entry.get("email")
    email = None
    if isinstance(email_raw, str) and email_raw.strip():
        email = common.normalize_email(email_raw)
    contact: dict = {"name": name}
    if phone:
        contact["phone"] = phone
        contact["e164"] = e164
    if email:
        contact["email"] = email
    notes = entry.get("notes")
    if isinstance(notes, str) and notes.strip():
        contact["notes"] = notes.strip()
    return contact


def add_contact(payload: dict) -> tuple[dict, bool]:
    contact = common.build_contact_record(payload)
    phone = str(contact.get("phone") or "")
    email = str(contact.get("email") or "")
    created = True
    with _history_lock:
        contacts = common.load_list(common.CONTACTS_PATH)
        for existing in contacts:
            existing_phone = str(existing.get("phone") or existing.get("number") or existing.get("e164") or "")
            existing_email = str(existing.get("email") or "").strip()
            phone_hit = bool(phone and existing_phone and common.numbers_match(existing_phone, phone))
            email_hit = bool(email and existing_email and common.emails_match(existing_email, email))
            if phone_hit or email_hit:
                # Preserve notes when the API omits the field.
                if "notes" not in payload and isinstance(existing.get("notes"), str) and existing["notes"].strip():
                    contact["notes"] = existing["notes"].strip()
                existing.clear()
                existing.update(contact)
                contact = public_contact(existing)
                created = False
                break
        if created:
            contacts.append(contact)
        common.save_json(common.CONTACTS_PATH, contacts)
    return public_contact(contact), created


def send_sms(payload: dict) -> dict:
    from_value = payload.get("from")
    to_value = payload.get("to") or payload.get("recipient")
    body = payload.get("body", payload.get("content"))
    if not isinstance(from_value, str) or not from_value.strip():
        raise ValueError("JSON must include 'from' matching a number in ownedPhoneNumbers.json")
    if not isinstance(to_value, str) or not to_value.strip():
        raise ValueError("JSON must include 'to' as a phone number")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("JSON must include a non-empty 'body'")

    owned = common.resolve_owned(common.load_owned(), from_value)
    to_e164_number = common.to_e164(to_value)
    parts = common.split_sms_body(body.strip())
    from_e164 = common.to_e164(owned["number"])
    records: list[dict] = []
    for part in parts:
        result = send_twilio_sms(owned, to_e164_number, part)
        now = datetime.now().astimezone()
        record = {
            "kind": "sms",
            "direction": "outbound",
            "from": from_e164,
            "from_sid": owned.get("sid"),
            "to": to_e164_number,
            "body": part,
            "status": result.get("status"),
            "provider": "twilio",
            "provider_sid": result.get("sid"),
            "created_at_unix": int(time.time()),
            "created_at_iso": now.isoformat(),
        }
        append_sent(record)
        records.append(record)
    if len(records) == 1:
        return records[0]
    return {"count": len(records), "messages": records}


def send_email(payload: dict) -> dict:
    from_value = payload.get("from")
    if not isinstance(from_value, str) or not from_value.strip():
        raise ValueError("JSON must include 'from' matching an address in ownedEmailAddresses.json")
    owned = common.resolve_owned_email(common.load_owned_emails(), from_value)
    message, recipients, meta = common.build_outbound_email(owned, payload)
    common.send_smtp_message(owned, message, recipients)
    now = datetime.now().astimezone()
    record = {
        "kind": "email",
        "direction": "outbound",
        "from": common.normalize_email(str(owned["address"])),
        "to": meta["to"],
        "cc": meta["cc"],
        "bcc": meta["bcc"],
        "subject": meta["subject"],
        "body": meta["body"],
        "attachments": meta["attachments"],
        "status": "sent",
        "provider": "smtp",
        "provider_sid": meta["message_id"],
        "message_id": meta["message_id"],
        "created_at_unix": int(time.time()),
        "created_at_iso": now.isoformat(),
    }
    append_sent_email(record)
    return record


def _as_id_list(value, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        out = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        return out
    raise ValueError(f"'{field_name}' must be a string or array of strings")


def mark_emails_read(payload: dict) -> dict:
    address_value = payload.get("address") or payload.get("to") or payload.get("from")
    if not isinstance(address_value, str) or not address_value.strip():
        raise ValueError("JSON must include 'address' matching an owned email")
    owned = common.resolve_owned_email(common.load_owned_emails(), address_value)
    address = common.normalize_email(str(owned["address"]))
    mailbox = payload.get("mailbox") or "INBOX"
    if not isinstance(mailbox, str) or not mailbox.strip():
        raise ValueError("'mailbox' must be a non-empty string when provided")
    mailbox = mailbox.strip()

    message_ids = _as_id_list(
        payload.get("message_ids") or payload.get("message_id") or payload.get("ids"),
        "message_ids",
    )
    uids = _as_id_list(payload.get("uids") or payload.get("uid") or payload.get("imap_uids"), "uids")
    provider_sids = _as_id_list(payload.get("provider_sids") or payload.get("provider_sid"), "provider_sids")

    if not message_ids and not uids and not provider_sids:
        raise ValueError("JSON must include message_ids, provider_sids, and/or uids")

    wanted_mids = {item for item in message_ids}
    wanted_sids = {item for item in provider_sids}
    wanted_uids = {item for item in uids}

    with _history_lock:
        history = common.load_list(common.RECEIVED_EMAIL_PATH)
        matched: list[dict] = []
        for item in history:
            if not common.emails_match(str(item.get("to", "")), address):
                continue
            mid = item.get("message_id")
            sid = item.get("provider_sid")
            uid = item.get("imap_uid")
            hit = False
            if isinstance(mid, str) and mid.strip() in wanted_mids:
                hit = True
            if isinstance(sid, str) and sid.strip() in wanted_sids:
                hit = True
            if isinstance(uid, str) and uid.strip() in wanted_uids:
                hit = True
            if hit:
                matched.append(item)

        if not matched and wanted_uids:
            # Allow marking by UID even before local history knows the message.
            matched = [{"imap_uid": uid, "to": address, "mailbox": mailbox} for uid in wanted_uids]

        if not matched:
            raise ValueError("no matching emails found to mark as read")

        resolve_uids: list[str] = []
        for item in matched:
            uid = item.get("imap_uid")
            if isinstance(uid, str) and uid.strip():
                resolve_uids.append(uid.strip())
            item_mailbox = item.get("mailbox")
            if isinstance(item_mailbox, str) and item_mailbox.strip():
                mailbox = item_mailbox.strip()

        if not resolve_uids:
            raise ValueError(
                "matched emails lack imap_uid; re-poll the inbox then retry mark-read"
            )

        # Unique preserve order
        seen_uid: set[str] = set()
        unique_uids: list[str] = []
        for uid in resolve_uids:
            if uid in seen_uid:
                continue
            seen_uid.add(uid)
            unique_uids.append(uid)

    marked_uids = common.mark_imap_read(owned, uids=unique_uids, mailbox=mailbox)

    with _history_lock:
        history = common.load_list(common.RECEIVED_EMAIL_PATH)
        marked_records: list[dict] = []
        marked_set = set(marked_uids)
        for item in history:
            if not common.emails_match(str(item.get("to", "")), address):
                continue
            uid = item.get("imap_uid")
            if isinstance(uid, str) and uid.strip() in marked_set:
                item["read"] = True
                marked_records.append(item)
        common.save_json(common.RECEIVED_EMAIL_PATH, history)

    return {
        "address": address,
        "mailbox": mailbox,
        "marked": len(marked_uids),
        "uids": marked_uids,
        "messages": [
            {
                "provider_sid": item.get("provider_sid"),
                "message_id": item.get("message_id"),
                "imap_uid": item.get("imap_uid"),
                "subject": item.get("subject"),
                "read": True,
            }
            for item in marked_records
        ],
    }


def explain_text(host: str, port: int) -> str:
    base = f"http://{host}:{port}"
    try:
        numbers = [common.public_owned(entry) for entry in common.load_owned()]
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
        addresses = [common.public_owned_email(entry) for entry in common.load_owned_emails()]
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
Phonebox is a localhost REST API for sending and reading real SMS (Twilio) and real email (IMAP/SMTP) on owned accounts, and for placing outbound AI phone calls (Twilio Voice + OpenAI Realtime). Any program or AI on this machine can call it. It is not a simulator: POST /send/text and POST /send/email send live messages; POST /call places a live phone call.

Base URL
{base}
Bound to localhost only. Do not assume it is reachable from the internet.

Human config UI
GET {base}/gui
Minimal HTML pages to add/edit/delete owned phones, owned emails, contacts, and the OpenAI API key. Prefer this for humans; prefer the JSON API below for programs and AIs.

Owned phone numbers
GET {base}/numbers
Use one of these as "from" when sending SMS or placing calls, and as <phone> when reading the SMS inbox. Current owned numbers:

{number_block}

Owned email addresses
GET {base}/addresses
Use one of these as "from" when sending email, and as <address> when reading an email inbox. Current owned addresses:

{address_block}

OpenAI (required for phone calls)
GET {base}/openai
Returns {{"configured": true/false, "realtime_model": "..."}}. Never returns the API key. Configure the key at GET {base}/gui/openai (writes gitignored openai.json).

Contacts
GET {base}/contacts
Returns saved people (name, optional phone/e164, optional email, optional notes). Each contact has at least a phone or an email. Use a contact's phone as "to" when sending SMS or placing a call. Use a contact's email address as "to" when sending email — do not pass the contact name to /send/email.

POST {base}/contact/add
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

Place outbound phone call (async)
POST {base}/call
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
- Returns HTTP 202 immediately with a call record including "id" and status "queued".
- Poll GET {base}/call/<id> until status is completed, failed, or canceled.
- Statuses: queued → tunneling → dialing → in_progress → completed | failed | canceled.
- On completion, "transcript" holds turn text and "answers" maps each question to an extracted answer.
- POST {base}/call/<id>/hangup ends an in-progress call.
- GET {base}/calls lists recent calls (newest first).
- Requires: OpenAI key configured, cloudflared on PATH, Voice enabled on the Twilio number.
- phonebox starts a Cloudflare Quick Tunnel for the call media WebSocket; you do not host a public server.

Send email
POST {base}/send/email
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
<phone> is the owned number whose inbox you want (the number texts were sent TO).

Optional cutoff:
GET {base}/received/<phone>/since/<time>
<time> is a unix timestamp (seconds) or an ISO-8601 datetime. Returns messages with created_at_unix strictly greater than that cutoff.

The SMS inbox is refreshed from Twilio when you call /received, and also in the background about every 15 seconds.

Read inbound email
GET {base}/received/email/<address>
<address> is the owned email whose inbox you want.

Optional cutoff:
GET {base}/received/email/<address>/since/<time>

Unread only (important for multi-agent handoff):
GET {base}/received/email/<address>?unread=1
GET {base}/received/email/<address>/since/<time>?unread=1

Unread means the message does not have IMAP \\Seen yet (and local history read=false). After you handle an email, mark it read so another AI instance will not process it again.

Mark email(s) read
POST {base}/email/read
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

History files (for humans; prefer the API)
- sent.history.json / received.history.json — SMS
- sent.email.history.json / received.email.history.json — email
- calls.history.json — outbound phone calls
Do not read auth tokens or passwords from ownedPhoneNumbers.json, ownedEmailAddresses.json, or openai.json. The API never returns credentials.

Typical AI workflow (SMS)
1. GET /numbers and pick an owned "from" number.
2. GET /contacts if you need a recipient by name.
3. POST /send/text with from, to, and body.
4. Note created_at_unix (or time.now) as a cursor.
5. Later GET /received/<from>/since/<cursor> to see replies.

Typical AI workflow (email)
1. GET /addresses and pick an owned "from" address.
2. GET /contacts if you need a recipient email by name (then use that email address, not the name).
3. GET /received/email/<address>?unread=1 and handle those messages.
4. POST /email/read with the message_ids (or uids) you handled.
5. POST /send/email with real recipient addresses when a reply is needed.

Typical AI workflow (phone call)
1. GET /openai and confirm configured=true (else tell the human to open /gui/openai).
2. GET /numbers and pick a Voice-capable owned "from" number.
3. POST /call with from, to, context, and questions.
4. Poll GET /call/<id> until status is completed, failed, or canceled.
5. Read answers and transcript from the final record.

Examples (curl)
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
- Only send SMS or place calls from numbers returned by /numbers.
- Only send email from addresses returned by /addresses.
- This sends real SMS, email, and phone calls. Do not spam. Confirm recipients before contacting them. AI phone calls may be regulated (e.g. TCPA); obtain consent where required.
- Phone numbers in URLs may include +. Prefer the E.164 form (+1...) or digits-only.
- After handling an email, mark it read so other agents skip it.
"""


def api_index() -> dict:
    return {
        "service": "phonebox",
        "listen": "localhost",
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
                numbers = [common.public_owned(entry) for entry in common.load_owned()]
                self._json(200, {"numbers": numbers})
                return
            if path == "/addresses":
                addresses = [
                    common.public_owned_email(entry) for entry in common.load_owned_emails()
                ]
                self._json(200, {"addresses": addresses})
                return
            if path == "/contacts":
                contacts = [public_contact(entry) for entry in load_contacts()]
                self._json(200, {"contacts": contacts})
                return
            if path == "/openai":
                self._json(
                    200,
                    {
                        "configured": common.openai_configured(),
                        "realtime_model": common.openai_realtime_model(),
                    },
                )
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
                contact, created = add_contact(payload)
                self._json(201 if created else 200, contact)
                return
            if path == "/email/read":
                payload = self._read_json_object()
                if payload is None:
                    return
                result = mark_emails_read(payload)
                self._json(200, result)
                return
            if path == "/send/text":
                payload = self._read_json_object()
                if payload is None:
                    return
                record = send_sms(payload)
                self._json(201, record)
                return
            if path == "/send/email":
                payload = self._read_json_object()
                if payload is None:
                    return
                record = send_email(payload)
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
            poll_inbound_sms()
        except Exception as error:
            print(f"poll before /received failed: {error}", flush=True)
        messages = received_for(phone, cutoff)
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
            poll_inbound_email()
        except Exception as error:
            print(f"poll before /received/email failed: {error}", flush=True)
        messages = received_email_for(address, cutoff, unread_only=unread_only)
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

    for path in (
        common.RECEIVED_PATH,
        common.SENT_PATH,
        common.RECEIVED_EMAIL_PATH,
        common.SENT_EMAIL_PATH,
        common.CONTACTS_PATH,
        common.CALLS_PATH,
    ):
        if not path.exists():
            common.save_json(path, [])
    common.ASSETS_PATH.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()
    poller = threading.Thread(target=poll_loop, args=(args.poll_interval, stop), daemon=True)
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
