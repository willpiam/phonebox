#!/usr/bin/env python3
"""One-shot CLI for phonebox actions (no always-on REST server required).

JSON on stdout; errors on stderr. Exit 0 on success, 1 on failure.
Outbound calls block until complete.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback

import actions
import call_media
import calls
import common
import server


def _print_json(payload) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _fail(message: str, *, code: int = 1) -> int:
    print(message, file=sys.stderr)
    return code


def cmd_explain(_args: argparse.Namespace) -> int:
    text = server.explain_text(server.DEFAULT_HOST, server.DEFAULT_PORT)
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def cmd_numbers(_args: argparse.Namespace) -> int:
    _print_json({"numbers": actions.list_numbers()})
    return 0


def cmd_addresses(_args: argparse.Namespace) -> int:
    _print_json({"addresses": actions.list_addresses()})
    return 0


def cmd_contacts(_args: argparse.Namespace) -> int:
    _print_json({"contacts": actions.list_contacts_public()})
    return 0


def cmd_openai(_args: argparse.Namespace) -> int:
    _print_json(actions.openai_status())
    return 0


def cmd_contact_add(args: argparse.Namespace) -> int:
    payload: dict = {"name": args.name}
    if args.phone:
        payload["phone"] = args.phone
    if args.email:
        payload["email"] = args.email
    if args.notes is not None:
        payload["notes"] = args.notes
    contact, _created = actions.add_contact(payload)
    _print_json(contact)
    return 0


def cmd_send_text(args: argparse.Namespace) -> int:
    record = actions.send_sms(
        {
            "from": args.from_value,
            "to": args.to,
            "body": args.body,
        }
    )
    _print_json(record)
    return 0


def cmd_send_email(args: argparse.Namespace) -> int:
    payload: dict = {
        "from": args.from_value,
        "to": args.to,
        "subject": args.subject,
        "body": args.body if args.body is not None else "",
    }
    if args.cc:
        payload["cc"] = args.cc
    if args.bcc:
        payload["bcc"] = args.bcc
    if args.attachment:
        payload["attachments"] = args.attachment
    record = actions.send_email(payload)
    _print_json(record)
    return 0


def cmd_email_read(args: argparse.Namespace) -> int:
    payload: dict = {"address": args.address}
    if args.message_id:
        payload["message_ids"] = args.message_id
    if args.uid:
        payload["uids"] = args.uid
    if args.provider_sid:
        payload["provider_sids"] = args.provider_sid
    if args.mailbox:
        payload["mailbox"] = args.mailbox
    result = actions.mark_emails_read(payload)
    _print_json(result)
    return 0


def cmd_received_sms(args: argparse.Namespace) -> int:
    owned = common.resolve_owned(common.load_owned(), args.phone)
    phone = common.to_e164(owned["number"])
    cutoff = common.parse_cutoff(args.since) if args.since is not None else None
    try:
        actions.poll_inbound_sms()
    except Exception as error:
        print(f"poll before received sms failed: {error}", file=sys.stderr)
    messages = actions.received_for(phone, cutoff)
    payload: dict = {"phone": phone, "count": len(messages), "messages": messages}
    if cutoff is not None:
        payload["since"] = cutoff
    _print_json(payload)
    return 0


def cmd_received_email(args: argparse.Namespace) -> int:
    owned = common.resolve_owned_email(common.load_owned_emails(), args.address)
    address = common.normalize_email(str(owned["address"]))
    cutoff = common.parse_cutoff(args.since) if args.since is not None else None
    unread_only = bool(args.unread)
    try:
        actions.poll_inbound_email()
    except Exception as error:
        print(f"poll before received email failed: {error}", file=sys.stderr)
    messages = actions.received_email_for(address, cutoff, unread_only=unread_only)
    payload: dict = {
        "address": address,
        "count": len(messages),
        "unread_only": unread_only,
        "messages": messages,
    }
    if cutoff is not None:
        payload["since"] = cutoff
    _print_json(payload)
    return 0


def cmd_call_place(args: argparse.Namespace) -> int:
    context: dict = {
        "files": list(args.file or []),
        "background": args.background or "",
        "additional": args.additional or "",
    }
    payload = {
        "from": args.from_value,
        "to": args.to,
        "context": context,
        "questions": list(args.question or []),
    }
    record = calls.create_call_record(payload)
    call_id = str(record["id"])
    print(f"call started id={call_id} status={record.get('status')}", file=sys.stderr)
    exit_code = 0
    try:
        try:
            final = calls.wait_for_call(call_id, timeout=args.timeout, poll_interval=1.0)
        except KeyboardInterrupt:
            print(f"\nhanging up call {call_id}", file=sys.stderr)
            try:
                final = calls.hangup_call(call_id)
            except Exception as error:
                return _fail(f"hangup failed: {error}")
            _print_json(final)
            return 130
        _print_json(final)
        if str(final.get("status") or "") == "failed":
            exit_code = 1
    finally:
        try:
            call_media.stop_media_server()
        except Exception:
            pass
    return exit_code


def cmd_call_status(args: argparse.Namespace) -> int:
    record = calls.get_call(args.call_id)
    if record is None:
        return _fail(f"unknown call id: {args.call_id}")
    _print_json(record)
    return 0


def cmd_calls(_args: argparse.Namespace) -> int:
    items = calls.list_calls()
    _print_json({"calls": items, "count": len(items)})
    return 0


def cmd_call_hangup(args: argparse.Namespace) -> int:
    record = calls.hangup_call(args.call_id)
    _print_json(record)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="phonebox CLI — SMS, email, and outbound AI calls without a REST server",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_explain = sub.add_parser("explain", help="Plain-text briefing for agents")
    p_explain.set_defaults(func=cmd_explain)

    p_numbers = sub.add_parser("numbers", help="List owned phone numbers")
    p_numbers.set_defaults(func=cmd_numbers)

    p_addresses = sub.add_parser("addresses", help="List owned email addresses")
    p_addresses.set_defaults(func=cmd_addresses)

    p_contacts = sub.add_parser("contacts", help="List saved contacts")
    p_contacts.set_defaults(func=cmd_contacts)

    p_openai = sub.add_parser("openai", help="Whether an OpenAI API key is configured")
    p_openai.set_defaults(func=cmd_openai)

    p_contact = sub.add_parser("contact", help="Contact commands")
    contact_sub = p_contact.add_subparsers(dest="contact_command", required=True)
    p_contact_add = contact_sub.add_parser("add", help="Add or update a contact")
    p_contact_add.add_argument("--name", required=True, help="Display name")
    p_contact_add.add_argument("--phone", help="Phone number")
    p_contact_add.add_argument("--email", help="Email address")
    p_contact_add.add_argument("--notes", help="Optional notes")
    p_contact_add.set_defaults(func=cmd_contact_add)

    p_send = sub.add_parser("send", help="Send SMS or email")
    send_sub = p_send.add_subparsers(dest="send_command", required=True)

    p_send_text = send_sub.add_parser("text", help="Send an SMS")
    p_send_text.add_argument("--from", dest="from_value", required=True, help="Owned from number")
    p_send_text.add_argument("--to", required=True, help="Recipient phone number")
    p_send_text.add_argument("--body", required=True, help="Message body")
    p_send_text.set_defaults(func=cmd_send_text)

    p_send_email = send_sub.add_parser("email", help="Send an email")
    p_send_email.add_argument("--from", dest="from_value", required=True, help="Owned from address")
    p_send_email.add_argument(
        "--to",
        action="append",
        required=True,
        help="Recipient address (repeatable)",
    )
    p_send_email.add_argument("--subject", required=True, help="Subject line")
    p_send_email.add_argument("--body", default="", help="Plain-text body")
    p_send_email.add_argument("--cc", action="append", help="CC address (repeatable)")
    p_send_email.add_argument("--bcc", action="append", help="BCC address (repeatable)")
    p_send_email.add_argument(
        "--attachment",
        action="append",
        help="Attachment path under the phonebox project (repeatable)",
    )
    p_send_email.set_defaults(func=cmd_send_email)

    p_email = sub.add_parser("email", help="Email inbox commands")
    email_sub = p_email.add_subparsers(dest="email_command", required=True)
    p_email_read = email_sub.add_parser("read", help="Mark email(s) read on IMAP")
    p_email_read.add_argument("--address", required=True, help="Owned email address")
    p_email_read.add_argument(
        "--message-id",
        action="append",
        dest="message_id",
        help="Message-ID to mark read (repeatable)",
    )
    p_email_read.add_argument(
        "--uid",
        action="append",
        help="IMAP UID to mark read (repeatable)",
    )
    p_email_read.add_argument(
        "--provider-sid",
        action="append",
        dest="provider_sid",
        help="provider_sid to mark read (repeatable)",
    )
    p_email_read.add_argument("--mailbox", help="IMAP mailbox (default INBOX)")
    p_email_read.set_defaults(func=cmd_email_read)

    p_received = sub.add_parser("received", help="Read inbound SMS or email")
    received_sub = p_received.add_subparsers(dest="received_command", required=True)

    p_received_sms = received_sub.add_parser("sms", help="Inbound SMS inbox")
    p_received_sms.add_argument("phone", help="Owned phone number")
    p_received_sms.add_argument(
        "--since",
        help="Unix timestamp or ISO-8601 cutoff (exclusive)",
    )
    p_received_sms.set_defaults(func=cmd_received_sms)

    p_received_email = received_sub.add_parser("email", help="Inbound email inbox")
    p_received_email.add_argument("address", help="Owned email address")
    p_received_email.add_argument(
        "--since",
        help="Unix timestamp or ISO-8601 cutoff (exclusive)",
    )
    p_received_email.add_argument(
        "--unread",
        action="store_true",
        help="Only messages without IMAP \\Seen",
    )
    p_received_email.set_defaults(func=cmd_received_email)

    p_call = sub.add_parser("call", help="Place, inspect, or hang up outbound AI calls")
    call_sub = p_call.add_subparsers(dest="call_command", required=False)
    # Default: place a call when flags are present without a subcommand.
    # We use a nested parser: `call` with optional subcommands status/hangup,
    # and placing a call is `call` with --from/--to (or `call place`).
    p_call_place = call_sub.add_parser("place", help="Place a call and block until done")
    _add_call_place_args(p_call_place)
    p_call_place.set_defaults(func=cmd_call_place)

    p_call_status = call_sub.add_parser("status", help="Get call status / transcript / answers")
    p_call_status.add_argument("call_id", help="Call id")
    p_call_status.set_defaults(func=cmd_call_status)

    p_call_hangup = call_sub.add_parser("hangup", help="Hang up an in-progress call")
    p_call_hangup.add_argument("call_id", help="Call id")
    p_call_hangup.set_defaults(func=cmd_call_hangup)

    # Also allow: python3 cli.py call --from ... --to ...
    _add_call_place_args(p_call)
    p_call.set_defaults(func=cmd_call_dispatch)

    p_calls = sub.add_parser("calls", help="List recent outbound calls")
    p_calls.set_defaults(func=cmd_calls)

    return parser


def _add_call_place_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="from_value", help="Owned from number")
    parser.add_argument("--to", help="Callee phone number")
    parser.add_argument("--background", default="", help="Call background context")
    parser.add_argument("--additional", default="", help="Additional free-text context")
    parser.add_argument(
        "--file",
        action="append",
        help="Context file path under the phonebox project (repeatable)",
    )
    parser.add_argument(
        "--question",
        action="append",
        help="Question to extract after the call (repeatable)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Max seconds to wait for the call to finish (default: no limit)",
    )


def cmd_call_dispatch(args: argparse.Namespace) -> int:
    """Handle `call` with optional subcommand or place-call flags."""
    sub = getattr(args, "call_command", None)
    if sub == "place" or (sub is None and args.from_value and args.to):
        if not args.from_value or not args.to:
            return _fail("call requires --from and --to (or use: call status|hangup <id>)")
        return cmd_call_place(args)
    if sub == "status":
        return cmd_call_status(args)
    if sub == "hangup":
        return cmd_call_hangup(args)
    if sub is None:
        return _fail("usage: call --from … --to … | call status <id> | call hangup <id> | call place …")
    return _fail(f"unknown call command: {sub}")


def main(argv: list[str] | None = None) -> int:
    actions.ensure_history_files()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as error:
        return _fail(str(error))
    except TimeoutError as error:
        return _fail(str(error))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as error:
        traceback.print_exc()
        return _fail(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
