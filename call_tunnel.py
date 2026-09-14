"""Cloudflare Quick Tunnel helper for Twilio Media Streams reachability."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import IO


_URL_RE = re.compile(
    r"https://[a-z0-9-]+\.trycloudflare\.com",
    re.IGNORECASE,
)


class TunnelError(RuntimeError):
    """Raised when cloudflared cannot be started or no public URL appears."""


@dataclass
class QuickTunnel:
    public_https: str
    process: subprocess.Popen
    _reader: threading.Thread

    @property
    def public_wss_host(self) -> str:
        host = self.public_https
        if host.startswith("https://"):
            host = host[len("https://") :]
        elif host.startswith("http://"):
            host = host[len("http://") :]
        return host.rstrip("/")

    def wss_url(self, path: str = "/media-stream") -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"wss://{self.public_wss_host}{path}"

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def cloudflared_available() -> bool:
    return shutil.which("cloudflared") is not None


def _drain(stream: IO[str] | None, lines: list[str], stop: threading.Event) -> None:
    if stream is None:
        return
    try:
        for line in stream:
            lines.append(line.rstrip("\n"))
            if stop.is_set():
                break
    except Exception:
        pass


def start_quick_tunnel(
    local_host: str,
    local_port: int,
    *,
    timeout: float = 45.0,
    use_http2: bool = False,
) -> QuickTunnel:
    """Start `cloudflared tunnel --url http://host:port` and wait for the public URL."""
    if not cloudflared_available():
        raise TunnelError(
            "cloudflared not found on PATH; install it from "
            "https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/"
        )

    target = f"http://{local_host}:{local_port}"
    command = ["cloudflared", "tunnel", "--url", target, "--no-autoupdate"]
    if use_http2:
        command.extend(["--protocol", "http2"])

    env = os.environ.copy()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    stop = threading.Event()
    reader = threading.Thread(
        target=_drain,
        args=(process.stdout, lines, stop),
        daemon=True,
        name="cloudflared-stdout",
    )
    reader.start()

    deadline = time.monotonic() + timeout
    public: str | None = None
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stop.set()
                blob = "\n".join(lines[-40:])
                raise TunnelError(
                    f"cloudflared exited early (code {process.returncode}): {blob or '(no output)'}"
                )
            for line in lines:
                match = _URL_RE.search(line)
                if match:
                    public = match.group(0)
                    break
            if public:
                break
            time.sleep(0.15)
        if not public:
            stop.set()
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
            blob = "\n".join(lines[-40:])
            raise TunnelError(
                f"timed out waiting for cloudflared public URL. output: {blob or '(none)'}"
            )
        return QuickTunnel(public_https=public, process=process, _reader=reader)
    except Exception:
        stop.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        raise
