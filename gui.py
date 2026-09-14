"""Minimal server-rendered config UI for phonebox."""

from __future__ import annotations

import html
import urllib.parse

import common

STYLE = """
body { font-family: sans-serif; margin: 1.5rem; max-width: 52rem; line-height: 1.4; }
body.wide { max-width: 72rem; }
nav { display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem 1rem; }
nav a { margin-right: 0; }
nav .logo {
  display: inline-flex; align-items: center; text-decoration: none; margin-right: 0.5rem;
}
nav .logo img { display: block; height: 2.5rem; width: auto; }
h1, h2 { font-weight: normal; }
fieldset { margin: 1rem 0; padding: 0.75rem 1rem 1rem; }
legend { padding: 0 0.25rem; }
label { display: block; margin: 0.4rem 0 0.15rem; }
input[type=text], input[type=password], input[type=number], input[type=email], textarea, select {
  width: 100%; max-width: 36rem; box-sizing: border-box;
}
select { padding: 0.25rem; font: inherit; }
textarea { min-height: 5rem; font: inherit; }
.secret { display: flex; gap: 0.5rem; align-items: center; max-width: 36rem; }
.secret input[type=password] { flex: 1; width: auto; max-width: none; }
.secret button { flex: 0 0 auto; }
.row { margin: 0.5rem 0; }
.actions { margin-top: 0.75rem; }
.actions button { margin-right: 0.5rem; }
.msg { margin: 1rem 0; padding: 0.5rem 0.75rem; border: 1px solid #999; }
.err { border-color: #900; }
.muted { color: #555; font-size: 0.9rem; }
hr { margin: 1.5rem 0; border: none; border-top: 1px solid #ccc; }
.gallery {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(9rem, 1fr));
  gap: 0.75rem;
  max-height: 70vh;
  overflow: auto;
  padding: 0.25rem 0;
}
.gallery button.tile {
  display: block; width: 100%; padding: 0; border: 1px solid #ccc;
  background: #f7f7f7; cursor: pointer; aspect-ratio: 1; overflow: hidden;
}
.gallery button.tile img {
  display: block; width: 100%; height: 100%; object-fit: cover;
}
.lightbox {
  display: none; position: fixed; inset: 0; z-index: 1000;
  background: rgba(0,0,0,0.75); align-items: center; justify-content: center;
  padding: 2rem; cursor: pointer;
}
.lightbox.open { display: flex; }
.lightbox-inner { position: relative; max-width: min(90vw, 56rem); max-height: 90vh; }
.lightbox-inner img {
  display: block; max-width: 90vw; max-height: 85vh; object-fit: contain;
  background: #111; cursor: default;
}
.gallery-name { display: block; font-size: 0.8rem; margin-top: 0.25rem; word-break: break-all; }
.existing-controls { margin: 0.5rem 0 0.75rem; }
.existing-controls button { margin-right: 0.5rem; }
.existing-list details {
  margin: 0.75rem 0;
  border: 1px solid #ccc;
  padding: 0;
  overflow: hidden;
}
.existing-list summary {
  cursor: pointer;
  font-weight: normal;
  margin: 0;
  padding: 0.65rem 0.85rem;
  list-style: none;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  box-sizing: border-box;
}
.existing-list summary::-webkit-details-marker { display: none; }
.existing-list summary::marker { content: ""; }
.existing-list summary::before {
  content: "▸";
  flex: 0 0 1rem;
  width: 1rem;
  text-align: center;
  line-height: 1;
}
.existing-list details[open] > summary::before { content: "▾"; }
.existing-list details[open] > summary {
  margin-bottom: 0;
  border-bottom: 1px solid #ccc;
}
.existing-list .existing-body { padding: 0.75rem 0.85rem 0.85rem; }
.existing-list details fieldset { margin: 0; border: none; padding: 0; }
.existing-list details fieldset legend { display: none; }
"""

SCRIPT = """
function copySecret(id) {
  var input = document.getElementById(id);
  if (!input) return;
  var value = input.value || '';
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(value).catch(function () {
      input.select();
      document.execCommand('copy');
    });
  } else {
    input.select();
    document.execCommand('copy');
  }
}
function openLightbox(src, name) {
  var box = document.getElementById('lightbox');
  var img = document.getElementById('lightbox-img');
  if (!box || !img) return;
  img.src = src;
  img.alt = name || '';
  box.classList.add('open');
}
function closeLightbox() {
  var box = document.getElementById('lightbox');
  var img = document.getElementById('lightbox-img');
  if (!box || !img) return;
  box.classList.remove('open');
  img.removeAttribute('src');
  img.alt = '';
}
function lightboxBackdropClick(event) {
  if (event.target.id !== 'lightbox-img') closeLightbox();
}
document.addEventListener('keydown', function (event) {
  if (event.key === 'Escape') closeLightbox();
});
function setExistingOpen(open) {
  document.querySelectorAll('.existing-list details').forEach(function (el) {
    el.open = !!open;
  });
}
"""

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}

_secret_id = 0


def _next_secret_id() -> str:
    global _secret_id
    _secret_id += 1
    return f"secret-{_secret_id}"


def _secret_field(
    *,
    label: str,
    name: str,
    value: str = "",
    required: bool = False,
) -> str:
    field_id = _next_secret_id()
    req = " required" if required else ""
    return f"""
<label for="{escape(field_id)}">{escape(label)}</label>
<div class="secret">
  <input type="password" id="{escape(field_id)}" name="{escape(name)}" value="{escape(value)}" autocomplete="off"{req}>
  <button type="button" onclick="copySecret('{escape(field_id)}')">Copy</button>
</div>
"""


def escape(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def page(
    title: str,
    body: str,
    message: str | None = None,
    error: str | None = None,
    *,
    wide: bool = False,
) -> str:
    notice = ""
    if error:
        notice += f'<p class="msg err">{escape(error)}</p>\n'
    if message:
        notice += f'<p class="msg">{escape(message)}</p>\n'
    body_class = ' class="wide"' if wide else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{STYLE}</style>
<script>{SCRIPT}</script>
</head>
<body{body_class}>
<nav>
  <a class="logo" href="/gui" title="phonebox">
    <img src="/assets/logo.jpg" alt="phonebox">
  </a>
  <a href="/gui">Home</a>
  <a href="/gui/phones">Phones</a>
  <a href="/gui/emails">Emails</a>
  <a href="/gui/contacts">Contacts</a>
  <a href="/gui/openai">OpenAI</a>
  <a href="/gui/gallery">Gallery</a>
  <a href="/gui/donate">Donate</a>
</nav>
<hr>
{notice}<h1>{escape(title)}</h1>
{body}
</body>
</html>
"""


def index_page(message: str | None = None, error: str | None = None) -> str:
    body = """
<p>Edit owned Twilio numbers, email accounts, contacts, and OpenAI settings stored in local JSON files.</p>
<ul>
  <li><a href="/gui/phones">Owned phone numbers</a></li>
  <li><a href="/gui/emails">Owned email addresses</a></li>
  <li><a href="/gui/contacts">Contacts</a></li>
  <li><a href="/gui/openai">OpenAI API key</a> (required for outbound phone calls)</li>
  <li><a href="/gui/gallery">Asset gallery</a></li>
  <li><a href="/gui/donate">Donate</a></li>
</ul>
<p class="muted">Forms POST to /gui/... endpoints on this server. Secrets stay in gitignored files.</p>
<hr>
<h2>Server</h2>
<form method="post" action="/gui/shutdown" onsubmit="return confirm('Shut down phonebox?');">
  <button type="submit">Shut down</button>
</form>
<p class="muted">Stops this phonebox process. Start it again from the terminal with <code>python3 server.py</code>.</p>
"""
    return page("phonebox gui", body, message=message, error=error)


def shutdown_page() -> str:
    return page(
        "phonebox stopped",
        """
<p>phonebox is shutting down.</p>
<p class="muted">You can close this tab. Restart later with <code>python3 server.py</code>.</p>
""",
    )


def donate_page(message: str | None = None, error: str | None = None) -> str:
    ens_url = "https://app.ens.domains/williamdoyle.eth"
    body = f"""
<p>If phonebox is useful, you can send a donation to the ENS name <strong>williamdoyle.eth</strong>.</p>
<p><a href="{escape(ens_url)}" target="_blank" rel="noopener noreferrer">williamdoyle.eth</a></p>
<p class="muted">Opens the ENS app profile for williamdoyle.eth, where you can send crypto or view payment details.</p>
"""
    return page("Donate", body, message=message, error=error)


def _form_value(form: dict[str, str], key: str, default: str = "") -> str:
    return (form.get(key) or default).strip()


def _parse_index(form: dict[str, str]) -> int | None:
    raw = _form_value(form, "index")
    if raw == "":
        return None
    try:
        index = int(raw)
    except ValueError as error:
        raise ValueError("index must be an integer") from error
    if index < 0:
        raise ValueError("index must be >= 0")
    return index


def _existing_controls() -> str:
    return """
<div class="existing-controls">
  <button type="button" onclick="setExistingOpen(true)">Expand all</button>
  <button type="button" onclick="setExistingOpen(false)">Collapse all</button>
</div>
"""


def _wrap_existing(summary: str, body: str, *, open_by_default: bool = False) -> str:
    open_attr = " open" if open_by_default else ""
    return f"""
<details class="existing-item"{open_attr}>
  <summary>{escape(summary)}</summary>
  <div class="existing-body">
  {body}
  </div>
</details>
"""


def phones_page(message: str | None = None, error: str | None = None) -> str:
    try:
        phones = common.load_owned_phones_list()
    except Exception as err:
        return page("Owned phones", "", error=str(err))

    parts = [
        '<p class="muted">Values are written to ownedPhoneNumbers.json.</p>',
        "<h2>Add phone</h2>",
        _phone_form(action="/gui/phones/save", submit="Add"),
        "<hr><h2>Existing</h2>",
    ]
    if not phones:
        parts.append("<p>No owned phones yet.</p>")
    else:
        parts.append(_existing_controls())
        parts.append('<div class="existing-list">')
        for index, entry in enumerate(phones):
            number = str(entry.get("number") or "").strip() or f"Phone #{index}"
            summary = f"Phone #{index} — {number}"
            parts.append(
                _wrap_existing(
                    summary,
                    _phone_form(
                        action="/gui/phones/save",
                        submit="Save",
                        index=index,
                        entry=entry,
                        delete=True,
                    ),
                )
            )
        parts.append("</div>")
    return page("Owned phones", "\n".join(parts), message=message, error=error)


def _phone_form(
    *,
    action: str,
    submit: str,
    index: int | None = None,
    entry: dict | None = None,
    delete: bool = False,
) -> str:
    entry = entry or {}
    index_field = (
        f'<input type="hidden" name="index" value="{escape(index)}">\n'
        if index is not None
        else ""
    )
    legend = f"Phone #{index}" if index is not None else "New phone"
    delete_form = ""
    if delete and index is not None:
        delete_form = f"""
<form method="post" action="/gui/phones/delete" class="row" onsubmit="return confirm('Delete this phone?');">
  <input type="hidden" name="index" value="{escape(index)}">
  <button type="submit">Delete</button>
</form>
"""
    return f"""
<fieldset>
  <legend>{escape(legend)}</legend>
  <form method="post" action="{escape(action)}">
    {index_field}
    <label>number</label>
    <input type="text" name="number" value="{escape(entry.get('number'))}" required>
    <label>sid</label>
    <input type="text" name="sid" value="{escape(entry.get('sid'))}" required>
    <label>account_sid</label>
    <input type="text" name="account_sid" value="{escape(entry.get('account_sid'))}" required>
    {_secret_field(label="auth_token", name="auth_token", value=str(entry.get("auth_token") or ""), required=True)}
    <div class="actions"><button type="submit">{escape(submit)}</button></div>
  </form>
  {delete_form}
</fieldset>
"""


def save_phone_from_form(form: dict[str, str]) -> str:
    entry = {
        "number": _form_value(form, "number"),
        "sid": _form_value(form, "sid"),
        "account_sid": _form_value(form, "account_sid"),
        "auth_token": _form_value(form, "auth_token"),
    }
    common.validate_owned_phone(entry)
    with with_history_lock():
        phones = common.load_owned_phones_list()
        index = _parse_index(form)
        if index is None:
            phones.append(entry)
            common.save_owned_phones(phones)
            return "Added phone."
        if index >= len(phones):
            raise ValueError(f"no phone at index {index}")
        phones[index] = entry
        common.save_owned_phones(phones)
        return f"Saved phone #{index}."


def delete_phone_from_form(form: dict[str, str]) -> str:
    index = _parse_index(form)
    if index is None:
        raise ValueError("index is required to delete")
    with with_history_lock():
        phones = common.load_owned_phones_list()
        if index >= len(phones):
            raise ValueError(f"no phone at index {index}")
        removed = phones.pop(index)
        common.save_owned_phones(phones)
        return f"Deleted phone {removed.get('number')!r}."


def emails_page(message: str | None = None, error: str | None = None) -> str:
    try:
        if common.OWNED_EMAILS_PATH.exists():
            raw = common.load_json(common.OWNED_EMAILS_PATH)
            emails = raw if isinstance(raw, list) else []
        else:
            emails = []
    except Exception as err:
        return page("Owned emails", "", error=str(err))

    parts = [
        '<p class="muted">Values are written to ownedEmailAddresses.json. '
        "Passwords show as dots; use Copy to put one on the clipboard. "
        "Either set shared password, or both IMAP and SMTP passwords. "
        "Blank password fields keep the current value when editing.</p>",
        "<h2>Add email</h2>",
        _email_form(action="/gui/emails/save", submit="Add"),
        "<hr><h2>Existing</h2>",
    ]
    if not emails:
        parts.append("<p>No owned emails yet.</p>")
    else:
        parts.append(_existing_controls())
        parts.append('<div class="existing-list">')
        for index, entry in enumerate(emails):
            if not isinstance(entry, dict):
                continue
            address = str(entry.get("address") or "").strip() or f"Email #{index}"
            summary = f"Email #{index} — {address}"
            parts.append(
                _wrap_existing(
                    summary,
                    _email_form(
                        action="/gui/emails/save",
                        submit="Save",
                        index=index,
                        entry=entry,
                        delete=True,
                    ),
                )
            )
        parts.append("</div>")
    return page("Owned emails", "\n".join(parts), message=message, error=error)


def _email_form(
    *,
    action: str,
    submit: str,
    index: int | None = None,
    entry: dict | None = None,
    delete: bool = False,
) -> str:
    entry = entry or {}
    imap = entry.get("imap") if isinstance(entry.get("imap"), dict) else {}
    smtp = entry.get("smtp") if isinstance(entry.get("smtp"), dict) else {}
    index_field = (
        f'<input type="hidden" name="index" value="{escape(index)}">\n'
        if index is not None
        else ""
    )
    legend = f"Email #{index}" if index is not None else "New email"
    delete_form = ""
    if delete and index is not None:
        delete_form = f"""
<form method="post" action="/gui/emails/delete" class="row" onsubmit="return confirm('Delete this email account?');">
  <input type="hidden" name="index" value="{escape(index)}">
  <button type="submit">Delete</button>
</form>
"""
    shared_password = str(entry.get("password") or "")
    imap_password = str(imap.get("password") or "")
    smtp_password = str(smtp.get("password") or "")
    return f"""
<fieldset>
  <legend>{escape(legend)}</legend>
  <form method="post" action="{escape(action)}">
    {index_field}
    <label>address</label>
    <input type="email" name="address" value="{escape(entry.get('address'))}" required>
    <label>display_name</label>
    <input type="text" name="display_name" value="{escape(entry.get('display_name'))}">
    {_secret_field(label="password (shared IMAP+SMTP)", name="password", value=shared_password)}
    <h2 style="font-size:1rem;margin:1rem 0 0.25rem;">IMAP</h2>
    <label>imap.host</label>
    <input type="text" name="imap_host" value="{escape(imap.get('host', 'imap.ipage.com'))}" required>
    <label>imap.port</label>
    <input type="number" name="imap_port" value="{escape(imap.get('port', 993))}" required>
    <label>imap.ssl</label>
    <input type="text" name="imap_ssl" value="{escape('true' if imap.get('ssl', True) else 'false')}" required>
    {_secret_field(label="imap.password (optional override)", name="imap_password", value=imap_password)}
    <h2 style="font-size:1rem;margin:1rem 0 0.25rem;">SMTP</h2>
    <label>smtp.host</label>
    <input type="text" name="smtp_host" value="{escape(smtp.get('host', 'smtp.ipage.com'))}" required>
    <label>smtp.port</label>
    <input type="number" name="smtp_port" value="{escape(smtp.get('port', 465))}" required>
    <label>smtp.ssl</label>
    <input type="text" name="smtp_ssl" value="{escape('true' if smtp.get('ssl', True) else 'false')}" required>
    {_secret_field(label="smtp.password (optional override)", name="smtp_password", value=smtp_password)}
    <div class="actions"><button type="submit">{escape(submit)}</button></div>
  </form>
  {delete_form}
</fieldset>
"""


def _parse_bool(value: str, field: str) -> bool:
    text = value.strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{field} must be true or false")


def _parse_port(value: str, field: str) -> int:
    try:
        port = int(value.strip())
    except ValueError as error:
        raise ValueError(f"{field} must be an integer") from error
    if port <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return port


def _build_email_entry(form: dict[str, str], existing: dict | None = None) -> dict:
    existing = existing or {}
    existing_imap = existing.get("imap") if isinstance(existing.get("imap"), dict) else {}
    existing_smtp = existing.get("smtp") if isinstance(existing.get("smtp"), dict) else {}

    password = _form_value(form, "password")
    imap_password = _form_value(form, "imap_password")
    smtp_password = _form_value(form, "smtp_password")

    if not password and existing:
        password = str(existing.get("password") or "")
    if not imap_password and existing_imap.get("password"):
        imap_password = str(existing_imap.get("password") or "")
    if not smtp_password and existing_smtp.get("password"):
        smtp_password = str(existing_smtp.get("password") or "")

    entry: dict = {
        "address": _form_value(form, "address"),
        "display_name": _form_value(form, "display_name"),
        "imap": {
            "host": _form_value(form, "imap_host"),
            "port": _parse_port(_form_value(form, "imap_port"), "imap.port"),
            "ssl": _parse_bool(_form_value(form, "imap_ssl", "true"), "imap.ssl"),
        },
        "smtp": {
            "host": _form_value(form, "smtp_host"),
            "port": _parse_port(_form_value(form, "smtp_port"), "smtp.port"),
            "ssl": _parse_bool(_form_value(form, "smtp_ssl", "true"), "smtp.ssl"),
        },
    }
    if not entry["display_name"]:
        entry.pop("display_name", None)

    if password:
        entry["password"] = password
    if imap_password:
        entry["imap"]["password"] = imap_password
    if smtp_password:
        entry["smtp"]["password"] = smtp_password

    # If shared password is set, drop protocol-specific unless user typed overrides this submit.
    if password and not _form_value(form, "imap_password") and not _form_value(form, "smtp_password"):
        entry["imap"].pop("password", None)
        entry["smtp"].pop("password", None)
        # Keep only shared password.
        if "password" not in entry and existing.get("password"):
            entry["password"] = existing["password"]

    common.validate_owned_email(entry)
    return entry


def save_email_from_form(form: dict[str, str]) -> str:
    with with_history_lock():
        emails = []
        if common.OWNED_EMAILS_PATH.exists():
            raw = common.load_json(common.OWNED_EMAILS_PATH)
            if not isinstance(raw, list):
                raise ValueError("ownedEmailAddresses.json must be a JSON array")
            emails = raw
        index = _parse_index(form)
        if index is None:
            entry = _build_email_entry(form)
            emails.append(entry)
            common.save_owned_emails(emails)
            return f"Added email {entry['address']!r}."
        if index >= len(emails) or not isinstance(emails[index], dict):
            raise ValueError(f"no email at index {index}")
        entry = _build_email_entry(form, existing=emails[index])
        emails[index] = entry
        common.save_owned_emails(emails)
        return f"Saved email #{index}."


def delete_email_from_form(form: dict[str, str]) -> str:
    index = _parse_index(form)
    if index is None:
        raise ValueError("index is required to delete")
    with with_history_lock():
        if not common.OWNED_EMAILS_PATH.exists():
            raise ValueError("ownedEmailAddresses.json does not exist")
        emails = common.load_json(common.OWNED_EMAILS_PATH)
        if not isinstance(emails, list):
            raise ValueError("ownedEmailAddresses.json must be a JSON array")
        if index >= len(emails):
            raise ValueError(f"no email at index {index}")
        removed = emails.pop(index)
        common.save_json(common.OWNED_EMAILS_PATH, emails)
        address = removed.get("address") if isinstance(removed, dict) else removed
        return f"Deleted email {address!r}."


def contacts_page(message: str | None = None, error: str | None = None) -> str:
    try:
        contacts = common.load_list(common.CONTACTS_PATH)
    except Exception as err:
        return page("Contacts", "", error=str(err))

    parts = [
        '<p class="muted">Values are written to contacts.json. '
        "Phone and email are both optional, but at least one is required. "
        "Sending mail still uses the address itself in /send/email, not the contact name.</p>",
        "<h2>Add contact</h2>",
        _contact_form(action="/gui/contacts/save", submit="Add"),
        "<hr><h2>Existing</h2>",
    ]
    if not contacts:
        parts.append("<p>No contacts yet.</p>")
    for index, entry in enumerate(contacts):
        if not isinstance(entry, dict):
            continue
        parts.append(
            _contact_form(
                action="/gui/contacts/save",
                submit="Save",
                index=index,
                entry=entry,
                delete=True,
            )
        )
    return page("Contacts", "\n".join(parts), message=message, error=error)


def _contact_form(
    *,
    action: str,
    submit: str,
    index: int | None = None,
    entry: dict | None = None,
    delete: bool = False,
) -> str:
    entry = entry or {}
    index_field = (
        f'<input type="hidden" name="index" value="{escape(index)}">\n'
        if index is not None
        else ""
    )
    legend = f"Contact #{index}" if index is not None else "New contact"
    delete_form = ""
    if delete and index is not None:
        delete_form = f"""
<form method="post" action="/gui/contacts/delete" class="row" onsubmit="return confirm('Delete this contact?');">
  <input type="hidden" name="index" value="{escape(index)}">
  <button type="submit">Delete</button>
</form>
"""
    return f"""
<fieldset>
  <legend>{escape(legend)}</legend>
  <form method="post" action="{escape(action)}">
    {index_field}
    <label>name</label>
    <input type="text" name="name" value="{escape(entry.get('name'))}" required>
    <label>phone (optional)</label>
    <input type="text" name="phone" value="{escape(entry.get('phone') or entry.get('number'))}">
    <label>email (optional)</label>
    <input type="email" name="email" value="{escape(entry.get('email'))}">
    <label>notes (optional)</label>
    <textarea name="notes" rows="4">{escape(entry.get('notes') or '')}</textarea>
    <div class="actions"><button type="submit">{escape(submit)}</button></div>
  </form>
  {delete_form}
</fieldset>
"""


def save_contact_from_form(form: dict[str, str]) -> str:
    entry = common.build_contact_record(
        {
            "name": _form_value(form, "name"),
            "phone": _form_value(form, "phone"),
            "email": _form_value(form, "email"),
            "notes": form.get("notes", ""),
        }
    )
    name = entry["name"]
    phone = str(entry.get("phone") or "")
    email = str(entry.get("email") or "")
    with with_history_lock():
        contacts = common.load_list(common.CONTACTS_PATH)
        index = _parse_index(form)
        if index is None:
            for existing in contacts:
                if not isinstance(existing, dict):
                    continue
                existing_phone = str(
                    existing.get("phone") or existing.get("number") or existing.get("e164") or ""
                )
                existing_email = str(existing.get("email") or "").strip()
                phone_hit = bool(
                    phone and existing_phone and common.numbers_match(existing_phone, phone)
                )
                email_hit = bool(
                    email and existing_email and common.emails_match(existing_email, email)
                )
                if phone_hit or email_hit:
                    existing.clear()
                    existing.update(entry)
                    common.save_json(common.CONTACTS_PATH, contacts)
                    return f"Updated contact {name!r}."
            contacts.append(entry)
            common.save_json(common.CONTACTS_PATH, contacts)
            return f"Added contact {name!r}."
        if index >= len(contacts) or not isinstance(contacts[index], dict):
            raise ValueError(f"no contact at index {index}")
        contacts[index] = entry
        common.save_json(common.CONTACTS_PATH, contacts)
        return f"Saved contact #{index}."


def delete_contact_from_form(form: dict[str, str]) -> str:
    index = _parse_index(form)
    if index is None:
        raise ValueError("index is required to delete")
    with with_history_lock():
        contacts = common.load_list(common.CONTACTS_PATH)
        if index >= len(contacts):
            raise ValueError(f"no contact at index {index}")
        removed = contacts.pop(index)
        common.save_json(common.CONTACTS_PATH, contacts)
        name = removed.get("name") if isinstance(removed, dict) else removed
        return f"Deleted contact {name!r}."


def list_asset_images() -> list[str]:
    assets = common.ASSETS_PATH
    if not assets.is_dir():
        return []
    names: list[str] = []
    for path in sorted(assets.iterdir(), key=lambda item: item.name.lower()):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            names.append(path.name)
    return names


def gallery_page(message: str | None = None, error: str | None = None) -> str:
    try:
        names = list_asset_images()
    except Exception as err:
        return page("Gallery", "", error=str(err), wide=True)

    parts = [
        '<p class="muted">Images from the local <code>assets/</code> folder. '
        "Click a tile to enlarge.</p>",
    ]
    if not names:
        parts.append("<p>No images in assets/ yet.</p>")
    else:
        tiles = []
        for name in names:
            src = "/assets/" + urllib.parse.quote(name)
            tiles.append(
                f"""<div>
  <button type="button" class="tile" onclick="openLightbox('{escape(src)}', '{escape(name)}')">
    <img src="{escape(src)}" alt="{escape(name)}" loading="lazy">
  </button>
  <span class="gallery-name">{escape(name)}</span>
</div>"""
            )
        parts.append('<div class="gallery">\n' + "\n".join(tiles) + "\n</div>")
        parts.append(
            """
<div id="lightbox" class="lightbox" onclick="lightboxBackdropClick(event)">
  <div class="lightbox-inner">
    <img id="lightbox-img" alt="">
  </div>
</div>
"""
        )
    return page("Gallery", "\n".join(parts), message=message, error=error, wide=True)


# Shared with server.py so GUI writes use the same mutex as history I/O.
history_lock = None


def with_history_lock():
    if history_lock is None:
        class _Null:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return _Null()
    return history_lock


def parse_form_body(raw: bytes, content_type: str | None) -> dict[str, str]:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype and ctype not in {"application/x-www-form-urlencoded", ""}:
        raise ValueError("GUI forms must post as application/x-www-form-urlencoded")
    parsed = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    return {key: (values[-1] if values else "") for key, values in parsed.items()}


def _select_field(
    *,
    label: str,
    name: str,
    options: list[str],
    selected: str,
) -> str:
    field_id = f"select-{name}"
    values = list(options)
    if selected and selected not in values:
        values = [selected, *values]
    option_html = []
    for value in values:
        is_selected = " selected" if value == selected else ""
        option_html.append(
            f'<option value="{escape(value)}"{is_selected}>{escape(value)}</option>'
        )
    return f"""
<label for="{escape(field_id)}">{escape(label)}</label>
<select id="{escape(field_id)}" name="{escape(name)}">
{''.join(option_html)}
</select>
"""


def openai_page(message: str | None = None, error: str | None = None) -> str:
    config = common.load_openai_config()
    configured = common.openai_configured()
    current_key = str(config.get("api_key") or "")
    model = str(config.get("realtime_model") or common.DEFAULT_REALTIME_MODEL)
    voice = str(config.get("realtime_voice") or common.DEFAULT_REALTIME_VOICE)
    models, model_warning = common.openai_realtime_model_ids(
        current_key if configured else None
    )
    if model not in models:
        models = [model, *models]
    voices = list(common.REALTIME_VOICES)
    if voice not in voices:
        voices = [voice, *voices]

    status = "configured" if configured else "not configured"
    masked = common.mask_api_key(current_key) if configured else "(not set)"
    warning_html = (
        f'<p class="msg err">{escape(model_warning)}</p>\n' if model_warning else ""
    )
    models_note = (
        "Model options are loaded from OpenAI <code>GET /v1/models</code> "
        "(ids containing <code>realtime</code>), with a small fallback list."
        if configured
        else "Model options show the fallback list until an API key is saved; "
        "after that, phonebox refreshes them from OpenAI."
    )
    existing_block = ""
    if configured:
        existing_body = f"""
<form method="post" action="/gui/openai/save">
  <fieldset>
    <legend>Edit configuration</legend>
    <p class="muted">Current API key: <code>{escape(masked)}</code>. Leave the key blank to keep it.</p>
    {_secret_field(label="api_key (optional replacement)", name="api_key", value="", required=False)}
    {_select_field(label="realtime_model", name="realtime_model", options=models, selected=model)}
    {_select_field(label="realtime_voice", name="realtime_voice", options=voices, selected=voice)}
    <div class="actions">
      <button type="submit">Save changes</button>
    </div>
  </fieldset>
</form>
<form method="post" action="/gui/openai/clear" class="row" onsubmit="return confirm('Clear the OpenAI API key?');">
  <button type="submit">Clear API key</button>
</form>
"""
        existing_block = (
            "<h2>Existing</h2>"
            + _existing_controls()
            + '<div class="existing-list">'
            + _wrap_existing(
                f"OpenAI — {masked} · {model} · {voice}",
                existing_body,
                open_by_default=True,
            )
            + "</div>"
        )
        setup_block = ""
    else:
        setup_block = f"""
<h2>Add configuration</h2>
<form method="post" action="/gui/openai/save">
  <fieldset>
    <legend>OpenAI</legend>
    {_secret_field(label="api_key", name="api_key", value="", required=True)}
    {_select_field(label="realtime_model", name="realtime_model", options=models, selected=model)}
    {_select_field(label="realtime_voice", name="realtime_voice", options=voices, selected=voice)}
    <div class="actions">
      <button type="submit">Save</button>
    </div>
  </fieldset>
</form>
"""

    body = f"""
<p>Outbound phone calls use the OpenAI Realtime API. Settings are stored in <code>openai.json</code> (gitignored).</p>
<p class="muted">Status: <strong>{escape(status)}</strong>. Current key: <code>{escape(masked)}</code>.</p>
{warning_html}
<p class="muted">{models_note} Voice options are OpenAI's documented built-in Realtime voices (there is no voices list API).</p>
{setup_block}
{existing_block}
<p class="muted">Phone calls also need <code>cloudflared</code> on PATH and a Voice-capable Twilio number.</p>
"""
    return page("OpenAI", body, message=message, error=error)


def save_openai_from_form(form: dict[str, str]) -> str:
    existing = common.load_openai_config()
    api_key = _form_value(form, "api_key")
    if not api_key:
        api_key = str(existing.get("api_key") or "")
    model = _form_value(form, "realtime_model") or common.DEFAULT_REALTIME_MODEL
    voice = _form_value(form, "realtime_voice") or common.DEFAULT_REALTIME_VOICE
    if not api_key.strip():
        raise ValueError(
            "api_key is required (or keep the existing key by leaving the field blank after configuring once)"
        )
    if voice not in common.REALTIME_VOICES and voice != str(
        existing.get("realtime_voice") or ""
    ).strip():
        raise ValueError(
            f"realtime_voice must be one of: {', '.join(common.REALTIME_VOICES)}"
        )
    common.save_openai_config(
        {
            "api_key": api_key.strip(),
            "realtime_model": model.strip(),
            "realtime_voice": voice.strip(),
        }
    )
    return "saved OpenAI settings"


def clear_openai_from_form(form: dict[str, str]) -> str:
    existing = common.load_openai_config()
    existing.pop("api_key", None)
    if existing:
        common.save_openai_config(existing)
    elif common.OPENAI_PATH.exists():
        common.OPENAI_PATH.unlink()
    return "cleared OpenAI API key"

ROUTES_GET = {
    "/gui": index_page,
    "/gui/phones": phones_page,
    "/gui/emails": emails_page,
    "/gui/contacts": contacts_page,
    "/gui/openai": openai_page,
    "/gui/gallery": gallery_page,
    "/gui/donate": donate_page,
}

ROUTES_POST = {
    "/gui/phones/save": ("/gui/phones", save_phone_from_form),
    "/gui/phones/delete": ("/gui/phones", delete_phone_from_form),
    "/gui/emails/save": ("/gui/emails", save_email_from_form),
    "/gui/emails/delete": ("/gui/emails", delete_email_from_form),
    "/gui/contacts/save": ("/gui/contacts", save_contact_from_form),
    "/gui/contacts/delete": ("/gui/contacts", delete_contact_from_form),
    "/gui/openai/save": ("/gui/openai", save_openai_from_form),
    "/gui/openai/clear": ("/gui/openai", clear_openai_from_form),
}
