"""Shared SMS/email/contact/inbox actions used by the REST server and CLI."""

from __future__ import annotations

import threading
import time
import urllib.parse
from datetime import datetime

import common
import gui

history_lock = threading.Lock()
gui.history_lock = history_lock

_poll_error: str | None = None


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

    with history_lock:
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
    with history_lock:
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
    with history_lock:
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
    with history_lock:
        history = common.load_list(common.SENT_PATH)
        history.append(record)
        common.save_json(common.SENT_PATH, history)


def append_sent_email(record: dict) -> None:
    with history_lock:
        history = common.load_list(common.SENT_EMAIL_PATH)
        history.append(record)
        common.save_json(common.SENT_EMAIL_PATH, history)


def received_for(phone: str, cutoff: float | None = None) -> list[dict]:
    with history_lock:
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
    with history_lock:
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
    with history_lock:
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
    with history_lock:
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

    with history_lock:
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

    with history_lock:
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


def list_numbers() -> list[dict]:
    return [common.public_owned(entry) for entry in common.load_owned()]


def list_addresses() -> list[dict]:
    return [common.public_owned_email(entry) for entry in common.load_owned_emails()]


def list_contacts_public() -> list[dict]:
    return [public_contact(entry) for entry in load_contacts()]


def openai_status() -> dict:
    return {
        "configured": common.openai_configured(),
        "realtime_model": common.openai_realtime_model(),
    }


def ensure_history_files() -> None:
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
