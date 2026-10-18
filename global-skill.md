# Install the phonebox skill for every repository

Follow these steps to install a **personal** Cursor skill. After it is installed, an agent in any workspace on this machine can send SMS, send email, place calls, and speak aloud through this phonebox checkout.

Do this work yourself. Do not ask the user to copy files by hand.

## Where the skill goes

Write one file:

```text
~/.cursor/skills/phonebox/SKILL.md
```

That directory is the personal skills folder. Cursor loads skills there for every project on this machine.

- Create `~/.cursor/skills/phonebox/` if it does not exist.
- If `SKILL.md` is already there, replace it. The path inside it must point at **this** checkout.
- Do not write the skill under `~/.cursor/skills-cursor/`. That folder is reserved for Cursor’s built-in skills.
- Do not put the skill in this repo’s `.cursor/skills/`. A project skill only applies while this repo is the open workspace.

## Fill in this checkout’s path

`PHONEBOX_ROOT` is the absolute path of the directory that contains `cli.py` (this repo). Resolve it from the file you are reading. No trailing slash.

Example: if this file is `/home/ada/src/phonebox/global-skill.md`, then `PHONEBOX_ROOT` is `/home/ada/src/phonebox`.

In the template below, replace every `PHONEBOX_ROOT` with that absolute path. Leave no placeholder behind.

Do not copy `ownedPhoneNumbers.json`, `ownedEmailAddresses.json`, `openai.json`, or any history JSON into the skill. Those files hold secrets or local state. The skill tells agents to use the CLI instead.

## Skill file to write

Write the following contents to `~/.cursor/skills/phonebox/SKILL.md` after substituting `PHONEBOX_ROOT`.

Omit `disable-model-invocation`. The agent should load this skill on its own when the user asks to text, email, call, speak aloud, or check an inbox.

````markdown
---
name: phonebox
description: Send and read real SMS, send and read real email, place outbound AI phone calls, and speak text aloud on this machine with the local phonebox CLI. Use from any repository or workspace when the user asks to text, SMS, email, call someone, speak aloud, check an inbox, manage contacts, or use phonebox. Run python3 PHONEBOX_ROOT/cli.py. Do not start the REST server and do not look for phonebox inside the current repo.
---

# Phonebox

Phonebox sends real SMS (Twilio), real email (IMAP/SMTP), and outbound AI phone calls (Twilio Voice + OpenAI Realtime) from this machine, and can speak text aloud on this machine's speakers (OpenAI TTS). It is not a simulator.

Use the CLI only. Do not start `server.py` and do not call `http://127.0.0.1:8765`. The server is a separate always-on process and is often not running. Every CLI command is one-shot and exits when finished.

This applies in every repository. Phonebox is not part of the open workspace. Do not search the current project for `cli.py`, and do not `cd` into the current repo expecting phonebox to be there.

## Run

Always invoke the absolute path. The shell's current directory can be anything.

```bash
python3 PHONEBOX_ROOT/cli.py <command>
```

JSON on stdout. Errors on stderr. Exit `0` success, `1` failure, `130` interrupted. If imports fail, run `pip install -r PHONEBOX_ROOT/requirements.txt` and retry.

Do not read `PHONEBOX_ROOT/ownedPhoneNumbers.json`, `ownedEmailAddresses.json`, or `openai.json`. They hold secrets. Use the list commands below; they omit credentials.

Discover what is configured before sending:

```bash
python3 PHONEBOX_ROOT/cli.py numbers
python3 PHONEBOX_ROOT/cli.py addresses
python3 PHONEBOX_ROOT/cli.py contacts
python3 PHONEBOX_ROOT/cli.py openai
```

- `numbers` — owned Twilio numbers. Use one as `--from` for SMS and calls, and as the phone argument when reading SMS.
- `addresses` — owned mailboxes. Use one as `--from` for email, and as the address argument when reading email.
- `contacts` — saved people (`name`, optional `phone` / `e164`, optional `email`, optional `notes`). Send to the phone or email, never the contact name.
- `openai` — `{"configured": true/false, "realtime_model": "..."}`. Required before placing a call or speaking. Never returns the key.

Phone numbers accept `(236) 243-8512`, `2362438512`, or `+12362438512`. Prefer E.164 (`+1...`) in commands.

## SMS

```bash
python3 PHONEBOX_ROOT/cli.py send text --from '+1OWNED' --to '+1RECIPIENT' --body 'hello'
python3 PHONEBOX_ROOT/cli.py received sms +1OWNED
python3 PHONEBOX_ROOT/cli.py received sms +1OWNED --since 1720000000
```

`--since` is a unix timestamp or ISO-8601 datetime. Messages with `created_at_unix` strictly greater than that cutoff are returned. `received sms` polls Twilio before listing.

`--from` must be an owned number. `--body` must be non-empty. Bodies longer than 1600 characters are split on word boundaries and sent as `(1/n)`, `(2/n)`, … parts. A split send returns `{"count": N, "messages": [...]}` instead of one record.

## Email

```bash
python3 PHONEBOX_ROOT/cli.py send email --from 'you@example.com' --to 'recipient@example.com' --subject 'hello' --body 'hi'
python3 PHONEBOX_ROOT/cli.py received email you@example.com --unread
python3 PHONEBOX_ROOT/cli.py received email you@example.com --since 1720000000
python3 PHONEBOX_ROOT/cli.py email read --address 'you@example.com' --message-id '<abc@example.com>'
```

- Repeat `--to`, `--cc`, `--bcc`, and `--attachment` for multiple values.
- `--attachment` may be an absolute path to a file in any repository.
- `--body` may be empty. `--subject` is required.
- `received email` polls IMAP first. Messages are oldest-first. Plain text is `body`. Large HTML is omitted; `has_html` is true when HTML was dropped. If there is little plain text, `html` is included.
- `--unread` returns messages that do not yet have IMAP `\Seen`. After you handle a message, mark it read so another agent does not process it again.
- `email read` accepts repeatable `--message-id`, `--uid`, or `--provider-sid`. At least one id is required. Optional `--mailbox` (default `INBOX`).
- Exit 0 means SMTP delivered the message. Do not send it again. A missing Sent folder is created automatically. If the JSON includes `sent_copy_error`, the copy into Sent failed; tell the user and do not retry the send.

## Contacts

```bash
python3 PHONEBOX_ROOT/cli.py contact add --name 'Ada Lovelace' --phone '519 555 1212' --email 'ada@example.com' --notes 'met at conference'
```

`--name` is required. At least one of `--phone` or `--email` is required. Adding an existing phone or email updates that contact.

## Phone calls

Outbound only. Inbound human-to-agent calls are not supported.

Before the first call in a session, confirm `python3 PHONEBOX_ROOT/cli.py openai` shows `"configured": true` and that `cloudflared` is on `PATH`. The from-number must have Voice enabled in Twilio. Phonebox opens a Cloudflare Quick Tunnel for the call; do not host a public server. If the key is missing, tell the user to write `{"api_key":"sk-..."}` into `PHONEBOX_ROOT/openai.json`. Do not start the REST server to collect the key.

```bash
python3 PHONEBOX_ROOT/cli.py call \
  --from '+1OWNED' \
  --to '+1RECIPIENT' \
  --background 'Why you are calling and what the callee should know.' \
  --additional 'Optional extra free text.' \
  --question 'What is their preferred callback time?' \
  --question 'Did they confirm the appointment?' \
  --file 'PHONEBOX_ROOT/notes/example.txt'
```

The command blocks until the call reaches `completed`, `failed`, or `canceled`, then prints the final record (`transcript`, `answers`). Stderr prints `call started id=...` immediately. Wait several minutes; there is no default timeout. Pass `--timeout SECONDS` to cap the wait. Ctrl+C hangs up and exits 130.

`--file` paths must stay inside `PHONEBOX_ROOT`. A file in the current repo cannot be passed directly; copy it into that directory first, then pass the copy. Each file is capped around 32 KB; combined context is capped around 48,000 characters.

```bash
python3 PHONEBOX_ROOT/cli.py calls
python3 PHONEBOX_ROOT/cli.py call status CALL_ID
python3 PHONEBOX_ROOT/cli.py call hangup CALL_ID
```

Statuses: `queued` → `tunneling` → `dialing` → `in_progress` → `completed` | `failed` | `canceled`. On completion, `answers` maps each `--question` to an extracted answer.

## Speak

Speak text aloud on this machine's speakers. It is not a phone call.

Before the first speak in a session, confirm `python3 PHONEBOX_ROOT/cli.py openai` shows `"configured": true`. Speak uses the same key as calls (`PHONEBOX_ROOT/openai.json`). If the key is missing, tell the user to write `{"api_key":"sk-..."}` into that file. Do not read `openai.json` and do not start the REST server to collect the key.

A local player must be on `PATH`. Phonebox uses the first one it finds: `paplay`, `ffplay`, or `mpv`.

```bash
python3 PHONEBOX_ROOT/cli.py speak --text 'The build finished.'
python3 PHONEBOX_ROOT/cli.py speak --text 'The build finished.' --voice nova
```

The command blocks until playback finishes, then prints `{"ok": true, "voice": "...", "model": "tts-1", "chars": N}`. `--text` is required and at most 4096 characters. `--voice` defaults to `alloy`. Allowed voices: `alloy`, `ash`, `ballad`, `coral`, `echo`, `fable`, `nova`, `onyx`, `sage`, `shimmer`.

Exit 0 means the audio finished playing.

## Workflows

SMS:

1. `numbers`, then `contacts` if you only have a name.
2. `send text`.
3. Keep `created_at_unix` from the sent record as a cursor.
4. Later, `received sms <from> --since <cursor>` for replies.

Email:

1. `addresses`, then `contacts` if you only have a name. Use the address, not the name.
2. `received email <address> --unread`.
3. Handle the message, then `email read`.
4. `send email` if a reply is needed.

Call:

1. `openai` (`configured` must be true) and `numbers`.
2. `call` with `--background` and any `--question` flags. Block until it exits.
3. Read `answers` and `transcript` from the JSON.

Speak:

1. `openai` (`configured` must be true).
2. `speak --text '…'`. Block until it exits.
3. Read `ok` from the JSON.

## Constraints

- These are real messages and real calls. Do not spam. Confirm the recipient with the user before contacting someone new.
- Only send SMS or place calls from numbers returned by `numbers`.
- Only send email from addresses returned by `addresses`.
- AI-initiated calls may be regulated (for example TCPA in the US). Only call numbers the user is allowed to contact.
- Do not read history JSON files by hand. Use the CLI so inboxes are polled first.
````

## Check the install

1. Open `~/.cursor/skills/phonebox/SKILL.md` and confirm every former `PHONEBOX_ROOT` is the absolute path of this checkout.
2. Confirm `cli.py` exists at that path.
3. Run `python3 PHONEBOX_ROOT/cli.py explain`. It should print JSON and exit 0. If imports fail, run `pip install -r PHONEBOX_ROOT/requirements.txt` and retry.
4. Tell the user the skill is installed and that agents in other repositories can text, email, call, and speak through this checkout. New chats pick the skill up from its description; a chat that was already open may need to be started again.
