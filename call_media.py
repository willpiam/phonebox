"""Local Twilio Media Streams WebSocket server bridged to OpenAI Realtime."""

from __future__ import annotations

import asyncio
import json
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.asyncio.server import ServerConnection

import common

OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"


@dataclass
class MediaSession:
    call_id: str
    instructions: str
    greeting_hint: str
    api_key: str
    model: str
    voice: str
    transcript: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None
    connected: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    cancel: threading.Event = field(default_factory=threading.Event)
    _twilio_ws: Any = field(default=None, repr=False)
    _openai_ws: Any = field(default=None, repr=False)
    _stream_sid: str | None = None
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)

    def append_transcript(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.transcript.append({"role": role, "text": text})

    def request_cancel(self) -> None:
        self.cancel.set()
        loop = self._loop
        twilio_ws = self._twilio_ws
        openai_ws = self._openai_ws
        if loop is None:
            return

        def _close() -> None:
            async def _do() -> None:
                for ws in (twilio_ws, openai_ws):
                    if ws is None:
                        continue
                    try:
                        await ws.close()
                    except Exception:
                        pass

            asyncio.create_task(_do())

        try:
            loop.call_soon_threadsafe(_close)
        except RuntimeError:
            pass


_lock = threading.Lock()
_sessions: dict[str, MediaSession] = {}
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_server = None
_ready = threading.Event()
_start_error: str | None = None


def register_session(session: MediaSession) -> None:
    with _lock:
        _sessions[session.call_id] = session


def unregister_session(call_id: str) -> None:
    with _lock:
        _sessions.pop(call_id, None)


def get_session(call_id: str) -> MediaSession | None:
    with _lock:
        return _sessions.get(call_id)


def media_server_ready() -> bool:
    return _ready.is_set() and _start_error is None


def media_server_error() -> str | None:
    return _start_error


def ensure_media_server(
    host: str = common.MEDIA_WS_HOST,
    port: int = common.MEDIA_WS_PORT,
) -> None:
    global _thread, _start_error
    if media_server_ready():
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            pass
        else:
            _ready.clear()
            _start_error = None
            _thread = threading.Thread(
                target=_run_media_thread,
                args=(host, port),
                daemon=True,
                name="phonebox-media-ws",
            )
            _thread.start()
    if not _ready.wait(timeout=15):
        raise RuntimeError(_start_error or "media WebSocket server failed to start")
    if _start_error:
        raise RuntimeError(_start_error)


def stop_media_server() -> None:
    global _server, _loop
    loop = _loop
    server = _server
    if loop is None:
        return

    def _stop() -> None:
        async def _do() -> None:
            if server is not None:
                server.close()
                await server.wait_closed()
            loop.stop()

        asyncio.ensure_future(_do())

    try:
        loop.call_soon_threadsafe(_stop)
    except RuntimeError:
        pass
    _ready.clear()


def _run_media_thread(host: str, port: int) -> None:
    global _loop, _server, _start_error
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop = loop

    async def _main() -> None:
        global _server, _start_error
        try:
            _server = await websockets.serve(
                _handle_connection,
                host,
                port,
                process_request=_process_request,
            )
            print(f"media websocket listening on ws://{host}:{port}/media-stream", flush=True)
            _ready.set()
            await asyncio.Future()
        except Exception as error:
            _start_error = str(error)
            _ready.set()
            traceback.print_exc()

    try:
        loop.run_until_complete(_main())
    except Exception as error:
        _start_error = str(error)
        _ready.set()
        traceback.print_exc()
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


async def _process_request(connection, request):
    # Allow only the media-stream path; websockets will complete the handshake.
    path = urlparse(request.path).path
    if path.rstrip("/") != "/media-stream":
        return connection.respond(404, "not found\n")
    return None


def _call_id_from_start(data: dict) -> str | None:
    start = data.get("start") or {}
    params = start.get("customParameters") or {}
    if isinstance(params, dict):
        value = params.get("call_id") or params.get("callId")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _call_id_from_path(path: str) -> str | None:
    query = parse_qs(urlparse(path).query)
    values = query.get("call_id") or []
    if values and values[0].strip():
        return values[0].strip()
    return None


async def _handle_connection(websocket: ServerConnection) -> None:
    path = websocket.request.path if websocket.request else "/media-stream"
    call_id_hint = _call_id_from_path(path)
    session: MediaSession | None = None
    stream_sid: str | None = None

    try:
        async for raw in websocket:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            data = json.loads(raw)
            event = data.get("event")
            if event == "start":
                stream_sid = (data.get("start") or {}).get("streamSid")
                call_id = _call_id_from_start(data) or call_id_hint
                if not call_id:
                    await websocket.close(code=1008, reason="missing call_id")
                    return
                session = get_session(call_id)
                if session is None:
                    await websocket.close(code=1008, reason="unknown call_id")
                    return
                session._twilio_ws = websocket
                session._stream_sid = stream_sid
                session._loop = asyncio.get_running_loop()
                session.connected.set()
                print(f"media stream started call_id={call_id} stream={stream_sid}", flush=True)
                await _bridge_openai(session, websocket, stream_sid)
                return
            if event == "connected":
                continue
            # Ignore media/stop before session is wired; bridge owns the rest.
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as error:
        traceback.print_exc()
        if session is not None:
            session.error = str(error)
    finally:
        if session is not None:
            session.finished.set()


async def _bridge_openai(
    session: MediaSession,
    twilio_ws: ServerConnection,
    stream_sid: str | None,
) -> None:
    url = f"{OPENAI_REALTIME_URL}?model={session.model}"
    try:
        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {session.api_key}"},
            max_size=8 * 1024 * 1024,
        ) as openai_ws:
            session._openai_ws = openai_ws
            await _initialize_session(openai_ws, session)

            async def twilio_to_openai() -> None:
                try:
                    async for raw in twilio_ws:
                        if session.cancel.is_set():
                            break
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="replace")
                        data = json.loads(raw)
                        event = data.get("event")
                        if event == "media":
                            payload = (data.get("media") or {}).get("payload")
                            if payload:
                                await openai_ws.send(
                                    json.dumps(
                                        {
                                            "type": "input_audio_buffer.append",
                                            "audio": payload,
                                        }
                                    )
                                )
                        elif event == "stop":
                            break
                except websockets.exceptions.ConnectionClosed:
                    pass
                finally:
                    try:
                        await openai_ws.close()
                    except Exception:
                        pass

            async def openai_to_twilio() -> None:
                nonlocal stream_sid
                try:
                    async for raw in openai_ws:
                        if session.cancel.is_set():
                            break
                        response = json.loads(raw)
                        rtype = response.get("type")
                        if rtype == "response.output_audio.delta" and response.get("delta"):
                            sid = stream_sid or session._stream_sid
                            if not sid:
                                continue
                            await twilio_ws.send(
                                json.dumps(
                                    {
                                        "event": "media",
                                        "streamSid": sid,
                                        "media": {"payload": response["delta"]},
                                    }
                                )
                            )
                        elif rtype == "input_audio_buffer.speech_started":
                            sid = stream_sid or session._stream_sid
                            try:
                                await openai_ws.send(json.dumps({"type": "response.cancel"}))
                            except Exception:
                                pass
                            if sid:
                                try:
                                    await twilio_ws.send(
                                        json.dumps({"event": "clear", "streamSid": sid})
                                    )
                                except Exception:
                                    pass
                        elif rtype == "response.output_audio_transcript.done":
                            session.append_transcript(
                                "assistant", str(response.get("transcript") or "")
                            )
                        elif rtype == "conversation.item.input_audio_transcription.completed":
                            session.append_transcript(
                                "user", str(response.get("transcript") or "")
                            )
                        elif rtype == "error":
                            err = response.get("error") or response
                            session.error = f"OpenAI Realtime error: {err}"
                            print(session.error, flush=True)
                except websockets.exceptions.ConnectionClosed:
                    pass
                finally:
                    try:
                        await twilio_ws.close()
                    except Exception:
                        pass

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())
    except Exception as error:
        session.error = str(error)
        traceback.print_exc()
        raise
    finally:
        session.finished.set()


async def _initialize_session(openai_ws, session: MediaSession) -> None:
    session_update = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": session.model,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {"type": "server_vad"},
                    "transcription": {"model": "whisper-1"},
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": session.voice,
                },
            },
            "instructions": session.instructions,
        },
    }
    await openai_ws.send(json.dumps(session_update))

    greeting = session.greeting_hint or (
        "Greet the person briefly, disclose that you are an AI assistant "
        "calling on behalf of someone, then work through your questions."
    )
    await openai_ws.send(
        json.dumps(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": greeting}],
                },
            }
        )
    )
    await openai_ws.send(json.dumps({"type": "response.create"}))


def build_instructions(
    *,
    background: str,
    additional: str,
    file_context: str,
    questions: list[str],
) -> str:
    parts = [
        "You are a phone-call agent placing an outbound call on behalf of another AI agent.",
        "Speak clearly and concisely. Always disclose that you are an AI assistant.",
        "Your job is to obtain answers to the listed questions using the provided context.",
        "Ask follow-up questions when answers are incomplete. Be polite and professional.",
        "When you have the information you need, thank the person and say goodbye.",
    ]
    if background.strip():
        parts.append("Background:\n" + background.strip())
    if additional.strip():
        parts.append("Additional context:\n" + additional.strip())
    if file_context.strip():
        parts.append("File context:\n" + file_context.strip())
    if questions:
        numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, start=1))
        parts.append("Questions to answer:\n" + numbered)
    return "\n\n".join(parts)
