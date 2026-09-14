"""Outbound phone call orchestration: history, Twilio dial, tunnel, media session."""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
import urllib.parse
from datetime import datetime
from typing import Any

import common
import call_media
import call_tunnel

_calls_lock = threading.Lock()
_active_tunnels: dict[str, call_tunnel.QuickTunnel] = {}
_active_owned: dict[str, dict] = {}


def load_calls() -> list[dict]:
    return common.load_list(common.CALLS_PATH)


def save_calls(calls: list[dict]) -> None:
    common.save_json(common.CALLS_PATH, calls)


def get_call(call_id: str) -> dict | None:
    with _calls_lock:
        for entry in load_calls():
            if entry.get("id") == call_id:
                return dict(entry)
    return None


def list_calls(limit: int = 50) -> list[dict]:
    with _calls_lock:
        calls = load_calls()
    calls_sorted = sorted(
        calls,
        key=lambda item: int(item.get("created_at_unix") or 0),
        reverse=True,
    )
    return calls_sorted[: max(0, limit)]


def update_call(call_id: str, **fields: Any) -> dict:
    with _calls_lock:
        calls = load_calls()
        for entry in calls:
            if entry.get("id") == call_id:
                entry.update(fields)
                entry["updated_at_unix"] = int(time.time())
                entry["updated_at_iso"] = datetime.now().astimezone().isoformat()
                save_calls(calls)
                return dict(entry)
    raise KeyError(f"unknown call id: {call_id}")


def append_call(record: dict) -> dict:
    with _calls_lock:
        calls = load_calls()
        calls.append(record)
        save_calls(calls)
        return dict(record)


def create_call_record(payload: dict) -> dict:
    from_value = payload.get("from")
    to_value = payload.get("to") or payload.get("recipient")
    if not isinstance(from_value, str) or not from_value.strip():
        raise ValueError("JSON must include 'from' matching a number in ownedPhoneNumbers.json")
    if not isinstance(to_value, str) or not to_value.strip():
        raise ValueError("JSON must include 'to' as a phone number")

    context = payload.get("context") or {}
    if context is None:
        context = {}
    if not isinstance(context, dict):
        raise ValueError("'context' must be an object when provided")

    questions_raw = payload.get("questions") or []
    if not isinstance(questions_raw, list):
        raise ValueError("'questions' must be an array of strings")
    questions: list[str] = []
    for item in questions_raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("'questions' entries must be non-empty strings")
        questions.append(item.strip())

    files_raw = context.get("files") or []
    if files_raw is None:
        files_raw = []
    if not isinstance(files_raw, list):
        raise ValueError("context.files must be an array of paths")
    files: list[str] = []
    for item in files_raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("context.files entries must be non-empty strings")
        files.append(item.strip())

    background = context.get("background") or ""
    additional = context.get("additional") or ""
    if background is None:
        background = ""
    if additional is None:
        additional = ""
    if not isinstance(background, str):
        raise ValueError("context.background must be a string")
    if not isinstance(additional, str):
        raise ValueError("context.additional must be a string")

    # Validate OpenAI + owned number early.
    api_key = common.openai_api_key()
    owned = common.resolve_owned(common.load_owned(), from_value)
    to_e164 = common.to_e164(to_value)
    from_e164 = common.to_e164(owned["number"])
    file_context = common.read_context_files(files) if files else ""

    now = datetime.now().astimezone()
    call_id = str(uuid.uuid4())
    record = {
        "id": call_id,
        "kind": "call",
        "direction": "outbound",
        "from": from_e164,
        "from_sid": owned.get("sid"),
        "to": to_e164,
        "status": "queued",
        "provider": "twilio",
        "provider_sid": None,
        "context": {
            "files": files,
            "background": background,
            "additional": additional,
        },
        "questions": questions,
        "transcript": [],
        "answers": None,
        "error": None,
        "tunnel_url": None,
        "created_at_unix": int(time.time()),
        "created_at_iso": now.isoformat(),
        "updated_at_unix": int(time.time()),
        "updated_at_iso": now.isoformat(),
    }
    append_call(record)
    _active_owned[call_id] = owned

    worker = threading.Thread(
        target=_run_call_worker,
        args=(
            call_id,
            owned,
            from_e164,
            to_e164,
            background,
            additional,
            file_context,
            questions,
            api_key,
        ),
        daemon=True,
        name=f"phonebox-call-{call_id[:8]}",
    )
    worker.start()
    return record


def hangup_call(call_id: str) -> dict:
    record = get_call(call_id)
    if record is None:
        raise ValueError(f"unknown call id: {call_id}")
    status = str(record.get("status") or "")
    if status in {"completed", "failed", "canceled"}:
        return record

    session = call_media.get_session(call_id)
    if session is not None:
        session.request_cancel()

    provider_sid = record.get("provider_sid")
    owned = _active_owned.get(call_id)
    if owned is None and record.get("from"):
        try:
            owned = common.resolve_owned(common.load_owned(), str(record["from"]))
        except Exception:
            owned = None
    if owned and isinstance(provider_sid, str) and provider_sid.strip():
        url = common.TWILIO_CALL.format(
            account_sid=owned["account_sid"],
            call_sid=provider_sid.strip(),
        )
        payload = urllib.parse.urlencode({"Status": "completed"}).encode("utf-8")
        try:
            common.twilio_request(owned, url, data=payload, method="POST")
        except Exception as error:
            print(f"hangup Twilio update failed for {call_id}: {error}", flush=True)

    return update_call(call_id, status="canceled")


def _run_call_worker(
    call_id: str,
    owned: dict,
    from_e164: str,
    to_e164: str,
    background: str,
    additional: str,
    file_context: str,
    questions: list[str],
    api_key: str,
) -> None:
    tunnel: call_tunnel.QuickTunnel | None = None
    try:
        call_media.ensure_media_server()
        update_call(call_id, status="tunneling")
        try:
            tunnel = call_tunnel.start_quick_tunnel(
                common.MEDIA_WS_HOST,
                common.MEDIA_WS_PORT,
                use_http2=False,
            )
        except call_tunnel.TunnelError:
            tunnel = call_tunnel.start_quick_tunnel(
                common.MEDIA_WS_HOST,
                common.MEDIA_WS_PORT,
                use_http2=True,
            )
        _active_tunnels[call_id] = tunnel
        stream_url = tunnel.wss_url("/media-stream")
        update_call(call_id, tunnel_url=tunnel.public_https, status="dialing")

        instructions = call_media.build_instructions(
            background=background,
            additional=additional,
            file_context=file_context,
            questions=questions,
        )
        session = call_media.MediaSession(
            call_id=call_id,
            instructions=instructions,
            greeting_hint=(
                "The callee just answered. Greet them briefly, disclose you are an AI "
                "assistant placing a call, then work through the questions using the "
                "provided context."
            ),
            api_key=api_key,
            model=common.openai_realtime_model(),
            voice=common.openai_realtime_voice(),
        )
        call_media.register_session(session)

        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response><Connect><Stream url=\""
            + stream_url
            + '"><Parameter name="call_id" value="'
            + call_id
            + '"/></Stream></Connect></Response>'
        )
        create_url = common.TWILIO_CALLS.format(account_sid=owned["account_sid"])
        form = urllib.parse.urlencode(
            {
                "From": from_e164,
                "To": to_e164,
                "Twiml": twiml,
            }
        ).encode("utf-8")
        result = common.twilio_request(owned, create_url, data=form)
        provider_sid = result.get("sid")
        update_call(
            call_id,
            provider_sid=provider_sid,
            status="in_progress",
        )

        # Wait for Twilio to open the media stream, then for the session to finish.
        if not session.connected.wait(timeout=120):
            session.request_cancel()
            update_call(
                call_id,
                status="failed",
                error="timed out waiting for Twilio Media Stream to connect",
            )
            return

        deadline = time.monotonic() + 15 * 60
        while time.monotonic() < deadline:
            if session.finished.wait(timeout=1.0):
                break
            current = get_call(call_id)
            if current and current.get("status") == "canceled":
                session.request_cancel()
                break
            if session.cancel.is_set():
                break
        else:
            session.request_cancel()
            update_call(call_id, status="failed", error="call timed out after 15 minutes")
            return

        transcript = list(session.transcript)
        error = session.error
        final_status = "canceled" if (get_call(call_id) or {}).get("status") == "canceled" else None
        answers = None
        if transcript and questions and not (final_status == "canceled"):
            try:
                answers = extract_answers(api_key, questions, transcript)
            except Exception as extract_error:
                print(f"answer extraction failed for {call_id}: {extract_error}", flush=True)
                if error is None:
                    error = f"call finished but answer extraction failed: {extract_error}"

        if final_status == "canceled":
            update_call(
                call_id,
                transcript=transcript,
                answers=answers,
                error=error,
            )
        elif error and not transcript:
            update_call(
                call_id,
                status="failed",
                transcript=transcript,
                answers=answers,
                error=error,
            )
        else:
            update_call(
                call_id,
                status="completed",
                transcript=transcript,
                answers=answers,
                error=error,
            )
    except Exception as error:
        traceback.print_exc()
        try:
            update_call(call_id, status="failed", error=str(error))
        except Exception:
            pass
    finally:
        call_media.unregister_session(call_id)
        tun = _active_tunnels.pop(call_id, None) or tunnel
        if tun is not None:
            try:
                tun.stop()
            except Exception:
                pass
        _active_owned.pop(call_id, None)


def extract_answers(
    api_key: str,
    questions: list[str],
    transcript: list[dict[str, str]],
) -> list[dict[str, str]]:
    lines = []
    for turn in transcript:
        role = turn.get("role") or "unknown"
        text = turn.get("text") or ""
        lines.append(f"{role}: {text}")
    transcript_text = "\n".join(lines)
    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, start=1))
    prompt = (
        "Extract answers to the questions from the phone-call transcript. "
        "Return JSON with key 'answers' as an array of objects "
        '{"question": "...", "answer": "..."} in the same order as the questions. '
        "If unknown, set answer to an empty string.\n\n"
        f"Questions:\n{numbered}\n\nTranscript:\n{transcript_text}"
    )
    payload = {
        "model": "gpt-4o-mini",
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "You extract structured answers from call transcripts.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    result = common.openai_chat_completions(api_key, payload)
    content = (
        ((result.get("choices") or [{}])[0].get("message") or {}).get("content") or "{}"
    )
    parsed = json.loads(content)
    answers_raw = parsed.get("answers")
    if not isinstance(answers_raw, list):
        # Fall back to aligning by index if model returned a dict.
        return [{"question": q, "answer": ""} for q in questions]
    out: list[dict[str, str]] = []
    for index, question in enumerate(questions):
        answer = ""
        if index < len(answers_raw) and isinstance(answers_raw[index], dict):
            value = answers_raw[index].get("answer")
            if isinstance(value, str):
                answer = value.strip()
            q = answers_raw[index].get("question")
            if isinstance(q, str) and q.strip():
                question = q.strip()
        out.append({"question": question, "answer": answer})
    return out
