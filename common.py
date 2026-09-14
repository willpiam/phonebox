"""Shared helpers for phonebox SMS and email send/receive."""

from __future__ import annotations

import base64
import email
import imaplib
import json
import mimetypes
import re
import smtplib
import ssl
import urllib.error
import urllib.request
from datetime import datetime
from email import encoders
from email.header import decode_header, make_header
from email.message import Message
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import (
    formataddr,
    formatdate,
    getaddresses,
    make_msgid,
    parseaddr,
    parsedate_to_datetime,
)
from pathlib import Path

HERE = Path(__file__).resolve().parent
OWNED_PHONES_PATH = HERE / "ownedPhoneNumbers.json"
# Back-compat alias if someone still has the old filename.
OWNED_PHONES_LEGACY_PATH = HERE / "owned.json"
OWNED_EMAILS_PATH = HERE / "ownedEmailAddresses.json"
CONTACTS_PATH = HERE / "contacts.json"
ASSETS_PATH = HERE / "assets"
RECEIVED_PATH = HERE / "received.history.json"
SENT_PATH = HERE / "sent.history.json"
RECEIVED_EMAIL_PATH = HERE / "received.email.history.json"
SENT_EMAIL_PATH = HERE / "sent.email.history.json"
OPENAI_PATH = HERE / "openai.json"
CALLS_PATH = HERE / "calls.history.json"
TWILIO_API_ROOT = "https://api.twilio.com"
TWILIO_MESSAGES = TWILIO_API_ROOT + "/2010-04-01/Accounts/{account_sid}/Messages.json"
TWILIO_CALLS = TWILIO_API_ROOT + "/2010-04-01/Accounts/{account_sid}/Calls.json"
TWILIO_CALL = TWILIO_API_ROOT + "/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
# Twilio Body parameter max for a single Messages API request (error 21617).
SMS_BODY_MAX = 1600
DEFAULT_REALTIME_MODEL = "gpt-realtime"
DEFAULT_REALTIME_VOICE = "alloy"
# Built-in Realtime voices (documented by OpenAI; no public list endpoint).
REALTIME_VOICES = [
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
]
# Used when no API key is set yet, or Models API is unreachable.
FALLBACK_REALTIME_MODELS = [
    "gpt-realtime",
    "gpt-realtime-1.5",
    "gpt-realtime-2.1",
]
MEDIA_WS_HOST = "127.0.0.1"
MEDIA_WS_PORT = 8766
CONTEXT_FILE_MAX_BYTES = 32_000
CONTEXT_TOTAL_MAX_CHARS = 48_000


def sms_part_prefix(index: int, total: int) -> str:
    return f"({index}/{total}) "


def _split_sms_chunks(text: str, max_len: int) -> list[str]:
    """Split text into chunks of at most max_len, preferring word boundaries."""
    if max_len < 1:
        raise ValueError("max_len must be at least 1")
    parts: list[str] = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        window = text[:max_len]
        cut = None
        for index in range(len(window) - 1, -1, -1):
            if window[index].isspace():
                cut = index
                break
        if cut is None:
            parts.append(window)
            text = text[max_len:].lstrip()
        else:
            chunk = text[:cut].rstrip()
            if not chunk:
                # Only leading whitespace in the window; hard-split to make progress.
                parts.append(window)
                text = text[max_len:].lstrip()
            else:
                parts.append(chunk)
                text = text[cut:].lstrip()
    return parts


def split_sms_body(body: str, max_len: int = SMS_BODY_MAX) -> list[str]:
    """Split text into SMS bodies of at most max_len, preferring word boundaries.

    Multi-part messages get a "(i/n) " prefix so the reader can reorder if the
    carrier delivers them out of sequence. Chunk size reserves room for that
    prefix. A single run of non-whitespace longer than the content budget is
    hard-split.
    """
    text = body.strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    total = len(_split_sms_chunks(text, max_len - len(sms_part_prefix(1, 9))))
    while True:
        prefix_budget = len(sms_part_prefix(total, total))
        content_max = max_len - prefix_budget
        if content_max < 1:
            raise ValueError("max_len is too small for part numbering prefixes")
        chunks = _split_sms_chunks(text, content_max)
        if len(chunks) == total:
            return [sms_part_prefix(i, total) + chunk for i, chunk in enumerate(chunks, start=1)]
        total = len(chunks)


def load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_list(path: Path) -> list:
    if not path.exists():
        return []
    data = load_json(path)
    if not isinstance(data, list):
        raise ValueError(f"{path.name} must be a JSON array")
    return data


def digits_only(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def to_e164(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("+"):
        digits = digits_only(stripped)
        if not digits:
            raise ValueError(f"invalid phone number: {value!r}")
        return "+" + digits
    digits = digits_only(stripped)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    raise ValueError(f"could not convert {value!r} to E.164")


def numbers_match(left: str, right: str) -> bool:
    try:
        return to_e164(left) == to_e164(right)
    except ValueError:
        return False


def normalize_email(value: str) -> str:
    stripped = value.strip()
    _, addr = parseaddr(stripped)
    candidate = (addr or stripped).strip().lower()
    if "@" not in candidate or candidate.startswith("@") or candidate.endswith("@"):
        raise ValueError(f"invalid email address: {value!r}")
    return candidate


def emails_match(left: str, right: str) -> bool:
    try:
        return normalize_email(left) == normalize_email(right)
    except ValueError:
        return False


def owned_phones_path() -> Path:
    if OWNED_PHONES_PATH.exists() or not OWNED_PHONES_LEGACY_PATH.exists():
        return OWNED_PHONES_PATH
    return OWNED_PHONES_LEGACY_PATH


def load_owned_phones_list() -> list[dict]:
    """Load owned phones for editing; empty list is allowed."""
    path = owned_phones_path()
    if not path.exists():
        return []
    owned = load_json(path)
    if not isinstance(owned, list):
        raise ValueError(f"{path.name} must be a JSON array")
    for entry in owned:
        if not isinstance(entry, dict):
            raise ValueError(f"{path.name} entries must be objects")
    return owned


def save_owned_phones(entries: list[dict]) -> None:
    save_json(OWNED_PHONES_PATH, entries)


def validate_owned_phone(entry: dict) -> None:
    number = entry.get("number")
    sid = entry.get("sid")
    account_sid = entry.get("account_sid")
    auth_token = entry.get("auth_token")
    if not isinstance(number, str) or not number.strip():
        raise ValueError("phone entry needs a non-empty 'number'")
    to_e164(number)
    if not isinstance(sid, str) or not sid.strip():
        raise ValueError("phone entry needs a non-empty 'sid'")
    if not isinstance(account_sid, str) or not account_sid.strip():
        raise ValueError("phone entry needs a non-empty 'account_sid'")
    if not isinstance(auth_token, str) or not auth_token.strip():
        raise ValueError("phone entry needs a non-empty 'auth_token'")


def load_owned() -> list[dict]:
    path = owned_phones_path()
    if not path.exists():
        raise ValueError(
            "ownedPhoneNumbers.json (or legacy owned.json) is missing; "
            "create it before starting the server"
        )
    owned = load_json(path)
    if not isinstance(owned, list) or not owned:
        raise ValueError(f"{path.name} must be a non-empty JSON array")
    return owned

def public_owned(entry: dict) -> dict:
    number = str(entry.get("number", "")).strip()
    return {
        "number": number,
        "e164": to_e164(number) if number else None,
        "sid": entry.get("sid"),
    }


def resolve_owned(owned: list, from_value: str) -> dict:
    wanted = from_value.strip()
    for entry in owned:
        number = str(entry.get("number", "")).strip()
        sid = str(entry.get("sid", "")).strip()
        if wanted == sid or (number and numbers_match(wanted, number)):
            return entry
    raise ValueError(f"no owned number matching {from_value!r} in ownedPhoneNumbers.json")


def _optional_password(value) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def email_password(entry: dict, protocol: str) -> str:
    """Resolve login password for imap or smtp.

    Prefer protocol-specific password when set; otherwise use top-level password.
    If top-level password is absent, both imap.password and smtp.password are required.
    """
    if protocol not in {"imap", "smtp"}:
        raise ValueError(f"unknown email protocol: {protocol!r}")
    section = entry.get(protocol)
    if not isinstance(section, dict):
        section = {}
    specific = _optional_password(section.get("password"))
    shared = _optional_password(entry.get("password"))
    if specific:
        return specific
    if shared:
        return shared
    other = "smtp" if protocol == "imap" else "imap"
    other_section = entry.get(other) if isinstance(entry.get(other), dict) else {}
    other_password = _optional_password(other_section.get("password"))
    if not other_password:
        raise ValueError(
            "owned email entry needs top-level 'password', or both "
            "imap.password and smtp.password"
        )
    raise ValueError(f"owned email entry needs {protocol}.password (or top-level password)")


def validate_owned_email(entry: dict) -> None:
    address = entry.get("address")
    if not isinstance(address, str) or not address.strip():
        raise ValueError("owned email entry needs a non-empty 'address'")
    normalize_email(address)
    for protocol in ("imap", "smtp"):
        section = entry.get(protocol)
        if not isinstance(section, dict):
            raise ValueError(f"owned email entry needs an '{protocol}' object")
        host = section.get("host")
        port = section.get("port")
        if not isinstance(host, str) or not host.strip():
            raise ValueError(f"owned email {protocol}.host is required")
        if not isinstance(port, int) or port <= 0:
            raise ValueError(f"owned email {protocol}.port must be a positive integer")
    # Force password rule validation for both protocols.
    email_password(entry, "imap")
    email_password(entry, "smtp")


def load_owned_emails() -> list[dict]:
    if not OWNED_EMAILS_PATH.exists():
        return []
    owned = load_json(OWNED_EMAILS_PATH)
    if not isinstance(owned, list):
        raise ValueError("ownedEmailAddresses.json must be a JSON array")
    for entry in owned:
        if not isinstance(entry, dict):
            raise ValueError("ownedEmailAddresses.json entries must be objects")
        validate_owned_email(entry)
    return owned


def save_owned_emails(entries: list[dict]) -> None:
    for entry in entries:
        validate_owned_email(entry)
    save_json(OWNED_EMAILS_PATH, entries)

def public_owned_email(entry: dict) -> dict:
    address = str(entry.get("address", "")).strip()
    return {
        "address": address,
        "normalized": normalize_email(address) if address else None,
        "display_name": entry.get("display_name") or None,
    }


def resolve_owned_email(owned: list, from_value: str) -> dict:
    wanted = from_value.strip()
    for entry in owned:
        address = str(entry.get("address", "")).strip()
        if address and emails_match(wanted, address):
            return entry
    raise ValueError(f"no owned email matching {from_value!r} in ownedEmailAddresses.json")


def contact_email_from_value(email) -> str | None:
    if email is None or (isinstance(email, str) and not email.strip()):
        return None
    if not isinstance(email, str):
        raise ValueError("'email' must be a string")
    return normalize_email(email)


def build_contact_record(payload: dict) -> dict:
    """Build a contact dict. Name required; phone and email optional but at least one required."""
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("must include a non-empty 'name'")
    phone_value = payload.get("phone") if "phone" in payload else payload.get("number")
    phone = ""
    if isinstance(phone_value, str):
        phone = phone_value.strip()
    elif phone_value is not None:
        raise ValueError("'phone' must be a string")
    email = contact_email_from_value(payload.get("email"))
    if not phone and not email:
        raise ValueError("contact needs at least one of 'phone' or 'email'")
    contact: dict = {"name": name.strip()}
    if phone:
        contact["phone"] = phone
        contact["e164"] = to_e164(phone)
    if email:
        contact["email"] = email
    if "notes" in payload:
        notes = payload.get("notes")
        if notes is None:
            pass
        elif not isinstance(notes, str):
            raise ValueError("'notes' must be a string")
        else:
            # Keep internal newlines; trim outer whitespace only.
            notes = notes.strip()
            if notes:
                contact["notes"] = notes
    return contact


def history_sids(history: list) -> set[str]:
    sids: set[str] = set()
    for record in history:
        sid = record.get("provider_sid")
        if isinstance(sid, str) and sid.strip():
            sids.add(sid.strip())
    return sids


def email_provider_sid(message_id: str | None, address: str, uid: str) -> str:
    if isinstance(message_id, str) and message_id.strip():
        return message_id.strip()
    return f"uid:{normalize_email(address)}:{uid.strip()}"


def twilio_basic_auth(account_sid: str, auth_token: str) -> str:
    token = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def twilio_request(
    owned: dict,
    url: str,
    data: bytes | None = None,
    method: str | None = None,
) -> dict:
    account_sid = owned.get("account_sid")
    auth_token = owned.get("auth_token")
    if not account_sid or not auth_token:
        raise ValueError("ownedPhoneNumbers.json entry needs account_sid and auth_token")
    if url.startswith("/"):
        url = TWILIO_API_ROOT + url
    http_method = method or ("POST" if data is not None else "GET")
    request = urllib.request.Request(
        url,
        data=data,
        method=http_method,
        headers={
            "Authorization": twilio_basic_auth(account_sid, auth_token),
        },
    )
    if data is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            if not body.strip():
                return {}
            return json.loads(body)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Twilio HTTP {error.code}: {detail}") from error


def openai_chat_completions(api_key: str, payload: dict, timeout: float = 60) -> dict:
    raw = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=raw,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI HTTP {error.code}: {detail}") from error


def openai_list_models(api_key: str, timeout: float = 30) -> list[dict]:
    request = urllib.request.Request(
        "https://api.openai.com/v1/models",
        method="GET",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI HTTP {error.code}: {detail}") from error
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def openai_realtime_model_ids(api_key: str | None = None) -> tuple[list[str], str | None]:
    """Return realtime model ids for the GUI dropdown.

    Prefers GET /v1/models filtered to ids containing 'realtime'. Falls back to a
    curated list when no key is available or the Models API fails.
    Returns (ids, warning_or_none).
    """
    warning: str | None = None
    discovered: list[str] = []
    key = (api_key or "").strip()
    if not key:
        try:
            key = openai_api_key()
        except ValueError:
            key = ""
    if key:
        try:
            for item in openai_list_models(key):
                model_id = item.get("id")
                if isinstance(model_id, str) and "realtime" in model_id.lower():
                    # Skip realtime transcription / special session models.
                    lowered = model_id.lower()
                    if "transcri" in lowered or "translation" in lowered:
                        continue
                    discovered.append(model_id)
        except Exception as error:
            warning = f"Could not load models from OpenAI ({error}); showing fallback list."
    ids = list(dict.fromkeys([*discovered, *FALLBACK_REALTIME_MODELS]))
    ids.sort()
    # Keep a stable preferred default near the top by reordering defaults first.
    preferred = [m for m in FALLBACK_REALTIME_MODELS if m in ids]
    rest = [m for m in ids if m not in preferred]
    return preferred + rest, warning


def mask_api_key(api_key: str) -> str:
    key = (api_key or "").strip()
    if not key:
        return "(not set)"
    if len(key) <= 12:
        return "*" * len(key)
    return f"{key[:7]}…{key[-4:]}"


def load_openai_config() -> dict:
    if not OPENAI_PATH.exists():
        return {}
    data = load_json(OPENAI_PATH)
    if not isinstance(data, dict):
        raise ValueError("openai.json must be a JSON object")
    return data


def save_openai_config(config: dict) -> None:
    save_json(OPENAI_PATH, config)


def openai_configured() -> bool:
    key = load_openai_config().get("api_key")
    return isinstance(key, str) and bool(key.strip())


def openai_api_key() -> str:
    key = load_openai_config().get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError(
            "OpenAI API key not configured; set it via /gui/openai or openai.json"
        )
    return key.strip()


def openai_realtime_model() -> str:
    model = load_openai_config().get("realtime_model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return DEFAULT_REALTIME_MODEL


def openai_realtime_voice() -> str:
    voice = load_openai_config().get("realtime_voice")
    if isinstance(voice, str) and voice.strip():
        return voice.strip()
    return DEFAULT_REALTIME_VOICE


def resolve_context_path(path_str: str) -> Path:
    """Resolve a context file path; must stay under the phonebox project directory."""
    if not isinstance(path_str, str) or not path_str.strip():
        raise ValueError("context file path must be a non-empty string")
    root = HERE.resolve()
    path = Path(path_str.strip()).expanduser()
    if path.is_absolute():
        candidate = path.resolve()
    else:
        candidate = (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(
            f"context file path must be under the phonebox directory: {path_str!r}"
        ) from error
    if not candidate.is_file():
        raise FileNotFoundError(f"context file not found: {path_str}")
    return candidate


def read_context_files(paths: list[str]) -> str:
    """Read and concatenate context files with size caps."""
    if not paths:
        return ""
    chunks: list[str] = []
    total = 0
    for path_str in paths:
        path = resolve_context_path(path_str)
        data = path.read_bytes()[:CONTEXT_FILE_MAX_BYTES]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        piece = f"--- file: {path.relative_to(HERE.resolve())} ---\n{text.strip()}\n"
        if total + len(piece) > CONTEXT_TOTAL_MAX_CHARS:
            remaining = CONTEXT_TOTAL_MAX_CHARS - total
            if remaining > 0:
                chunks.append(piece[:remaining] + "\n...[truncated]...\n")
            break
        chunks.append(piece)
        total += len(piece)
    return "\n".join(chunks).strip()


def parse_twilio_date(value: str | None) -> datetime:
    if not value:
        return datetime.now().astimezone()
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed.astimezone()


def parse_cutoff(value: str) -> float:
    stripped = value.strip()
    if not stripped:
        raise ValueError("cutoff time is empty")
    if stripped.isdigit():
        return float(int(stripped))
    try:
        return float(stripped)
    except ValueError:
        pass
    parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def part_charset(part: Message) -> str:
    return part.get_content_charset() or "utf-8"


def decode_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part_charset(part)
    for encoding in (charset, "utf-8", "latin-1"):
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def extract_bodies(message: Message) -> tuple[str, str, list[str]]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            content_disposition = (part.get_content_disposition() or "").lower()
            content_type = part.get_content_type()
            filename = part.get_filename()
            if filename:
                attachments.append(decode_header_value(filename))
                continue
            if content_disposition == "attachment":
                continue
            if content_type == "text/plain":
                text_parts.append(decode_payload(part))
            elif content_type == "text/html":
                html_parts.append(decode_payload(part))
    else:
        content_type = message.get_content_type()
        body = decode_payload(message)
        if content_type == "text/html":
            html_parts.append(body)
        else:
            text_parts.append(body)

    return "\n".join(text_parts).strip(), "\n".join(html_parts).strip(), attachments


def parse_address_list(value: str | None) -> list[str]:
    if not value:
        return []
    results: list[str] = []
    for _, addr in getaddresses([value]):
        if addr and addr.strip():
            results.append(addr.strip())
    return results


def as_address_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    raise ValueError("address fields must be a string or array of strings")


def as_path_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    raise ValueError("attachments must be a string or array of strings")


def flags_include_seen(flags: object) -> bool:
    if isinstance(flags, (bytes, bytearray)):
        text = flags.decode("utf-8", errors="replace")
    else:
        text = str(flags or "")
    return "\\Seen" in text or "\\seen" in text.lower()


def parse_imap_flags(payload: object) -> str:
    if isinstance(payload, (bytes, bytearray)):
        text = payload.decode("utf-8", errors="replace")
    else:
        text = str(payload or "")
    match = re.search(r"FLAGS\s+(\([^)]*\))", text, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return text


def parse_email_date(value: str | None) -> datetime:
    if not value:
        return datetime.now().astimezone()
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return datetime.now().astimezone()
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed.astimezone()


def imap_connect(entry: dict) -> imaplib.IMAP4:
    imap_settings = entry["imap"]
    host = str(imap_settings["host"]).strip()
    port = int(imap_settings["port"])
    use_ssl = bool(imap_settings.get("ssl", True))
    password = email_password(entry, "imap")
    address = str(entry["address"]).strip()
    if use_ssl:
        client: imaplib.IMAP4 = imaplib.IMAP4_SSL(
            host, port, ssl_context=ssl.create_default_context()
        )
    else:
        client = imaplib.IMAP4(host, port)
        client.starttls(ssl_context=ssl.create_default_context())
    client.login(address, password)
    return client


def message_to_email_record(
    *,
    owned: dict,
    uid: str,
    raw: bytes,
    flags: str,
    mailbox: str = "INBOX",
) -> dict:
    parsed = email.message_from_bytes(raw)
    text, html, attachments = extract_bodies(parsed)
    message_id = decode_header_value(parsed.get("Message-ID")).strip() or None
    date_header = decode_header_value(parsed.get("Date"))
    sent_at = parse_email_date(date_header)
    to_address = normalize_email(str(owned["address"]))
    from_header = decode_header_value(parsed.get("From"))
    _, from_addr = parseaddr(from_header)
    record = {
        "kind": "email",
        "direction": "inbound",
        "from": from_addr.strip() if from_addr else from_header,
        "from_header": from_header,
        "to": to_address,
        "cc": parse_address_list(decode_header_value(parsed.get("Cc"))),
        "subject": decode_header_value(parsed.get("Subject")),
        "body": text,
        "mailbox": mailbox,
        "imap_uid": uid,
        "read": flags_include_seen(flags),
        "provider": "imap",
        "provider_sid": email_provider_sid(message_id, to_address, uid),
        "message_id": message_id,
        "created_at_unix": int(sent_at.timestamp()),
        "created_at_iso": sent_at.isoformat(),
        "attachments": attachments,
    }
    # Prefer plain text for agents; include HTML only when useful (no plain body,
    # or short plain body that is likely a multipart stub).
    if html and (not text or len(text) < 40):
        record["html"] = html
    elif html:
        record["has_html"] = True
    return record


def _imap_fetch_raw_and_flags(client: imaplib.IMAP4, uid_bytes: bytes) -> tuple[bytes | None, str]:
    status, fetched = client.uid("fetch", uid_bytes, "(FLAGS BODY.PEEK[])")
    if status != "OK" or not fetched:
        return None, ""
    raw = None
    flags = ""
    for item in fetched:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        meta, payload = item[0], item[1]
        if isinstance(payload, (bytes, bytearray)):
            raw = bytes(payload)
            flags = parse_imap_flags(meta)
            break
    return raw, flags


def _imap_fetch_flags(client: imaplib.IMAP4, uid_bytes: bytes) -> str:
    status, fetched = client.uid("fetch", uid_bytes, "(FLAGS)")
    if status != "OK" or not fetched:
        return ""
    for item in fetched:
        if isinstance(item, tuple) and item:
            return parse_imap_flags(item[0])
        return parse_imap_flags(item)
    return ""


def fetch_imap_messages(
    owned: dict,
    mailbox: str = "INBOX",
    *,
    known_uids: set[str] | None = None,
) -> list[dict]:
    """Fetch inbox messages.

    Unknown UIDs download full bodies (BODY.PEEK so \\Seen is not set).
    Known UIDs only refresh FLAGS so read state stays in sync cheaply.
    """
    known = known_uids or set()
    client = imap_connect(owned)
    try:
        status, _ = client.select(mailbox, readonly=True)
        if status != "OK":
            raise RuntimeError(f"could not select mailbox {mailbox!r}")
        status, data = client.uid("search", None, "ALL")
        if status != "OK":
            raise RuntimeError("IMAP search failed")
        uids = data[0].split() if data and data[0] else []
        messages: list[dict] = []
        for uid_bytes in uids:
            uid = uid_bytes.decode("ascii", errors="replace")
            if uid in known:
                flags = _imap_fetch_flags(client, uid_bytes)
                messages.append(
                    {
                        "kind": "email",
                        "direction": "inbound",
                        "to": normalize_email(str(owned["address"])),
                        "mailbox": mailbox,
                        "imap_uid": uid,
                        "read": flags_include_seen(flags),
                        "provider": "imap",
                        # Placeholder sid; poll merge matches on uid for known mail.
                        "provider_sid": f"uid:{normalize_email(str(owned['address']))}:{uid}",
                    }
                )
                continue
            raw, flags = _imap_fetch_raw_and_flags(client, uid_bytes)
            if raw is None:
                continue
            messages.append(
                message_to_email_record(
                    owned=owned,
                    uid=uid,
                    raw=raw,
                    flags=flags,
                    mailbox=mailbox,
                )
            )
        return messages
    finally:
        try:
            client.logout()
        except Exception:
            pass


def mark_imap_read(owned: dict, *, uids: list[str], mailbox: str = "INBOX") -> list[str]:
    if not uids:
        return []
    client = imap_connect(owned)
    marked: list[str] = []
    try:
        status, _ = client.select(mailbox, readonly=False)
        if status != "OK":
            raise RuntimeError(f"could not select mailbox {mailbox!r}")
        for uid in uids:
            uid = str(uid).strip()
            if not uid:
                continue
            status, _ = client.uid("store", uid, "+FLAGS", "(\\Seen)")
            if status != "OK":
                raise RuntimeError(f"IMAP could not mark uid {uid} as seen")
            marked.append(uid)
        return marked
    finally:
        try:
            client.logout()
        except Exception:
            pass


def resolve_attachment(path_str: str) -> Path:
    path = Path(path_str).expanduser()
    candidates = []
    if path.is_absolute():
        candidates.append(path)
    else:
        candidates.extend([(HERE / path).resolve(), Path.cwd() / path])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"attachment not found: {path_str}")


def attach_file(message: MIMEMultipart, path: Path) -> None:
    ctype, encoding = mimetypes.guess_type(path.name)
    if ctype is None or encoding is not None:
        ctype = "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)
    part = MIMEBase(maintype, subtype)
    part.set_payload(path.read_bytes())
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=path.name)
    message.attach(part)


def build_outbound_email(owned: dict, payload: dict) -> tuple[MIMEMultipart, list[str], dict]:
    to_addrs = as_address_list(payload.get("to"))
    cc_addrs = as_address_list(payload.get("cc"))
    bcc_addrs = as_address_list(payload.get("bcc"))
    if not to_addrs:
        raise ValueError("JSON must include at least one 'to' address")
    subject = payload.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("JSON must include a non-empty 'subject'")
    body = payload.get("body", payload.get("content"))
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise ValueError("JSON 'body' must be a string")
    attachments = [resolve_attachment(item) for item in as_path_list(payload.get("attachments"))]

    from_addr = str(owned["address"]).strip()
    message = MIMEMultipart()
    message["From"] = formataddr((str(owned.get("display_name") or ""), from_addr))
    message["To"] = ", ".join(to_addrs)
    if cc_addrs:
        message["Cc"] = ", ".join(cc_addrs)
    message["Subject"] = subject.strip()
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=from_addr.split("@", 1)[-1])
    message.attach(MIMEText(body, "plain", "utf-8"))
    for path in attachments:
        attach_file(message, path)

    recipients = to_addrs + cc_addrs + bcc_addrs
    meta = {
        "to": to_addrs,
        "cc": cc_addrs,
        "bcc": bcc_addrs,
        "subject": subject.strip(),
        "body": body,
        "attachments": [str(path) for path in attachments],
        "message_id": message["Message-ID"],
    }
    return message, recipients, meta


def send_smtp_message(owned: dict, message: MIMEMultipart, recipients: list[str]) -> None:
    smtp_settings = owned["smtp"]
    host = str(smtp_settings["host"]).strip()
    port = int(smtp_settings["port"])
    use_ssl = bool(smtp_settings.get("ssl", True))
    password = email_password(owned, "smtp")
    address = str(owned["address"]).strip()
    context = ssl.create_default_context()
    if use_ssl:
        client = smtplib.SMTP_SSL(host, port, context=context)
    else:
        client = smtplib.SMTP(host, port)
        client.starttls(context=context)
    try:
        client.login(address, password)
        client.sendmail(address, recipients, message.as_string())
    finally:
        try:
            client.quit()
        except Exception:
            pass
