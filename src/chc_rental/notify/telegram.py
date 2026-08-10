"""Outbound-only Telegram sender.

This calls `sendMessage` and nothing else. There is no `getUpdates`, no webhook
and no polling of any kind, which is both the project's standing architecture
rule and the reason this bot can be shared with another project that *does*
poll it — `sendMessage` and `getUpdates` do not contend.

Two environment facts are baked in deliberately:

*   The token field is `repr=False`. The retired RentCast adapter put a
    credential in a plain dataclass, so it surfaced in tracebacks, logs and
    `pytest --showlocals`. Nothing here may repeat that.
*   TLS on this machine is intercepted by a locally-trusted root, so Python's
    bundled CA store rejects `api.telegram.org`. The macOS system bundle at
    `/etc/ssl/cert.pem` contains that root and works. Verification is never
    disabled — that would turn a local trust gap into a real interception risk
    on a connection carrying a bot token.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

TELEGRAM_API = "https://api.telegram.org"
_SYSTEM_CA_BUNDLES = ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem")


class TelegramSendError(RuntimeError):
    """Raised when a message could not be delivered.

    The pipeline treats a raised send as a failure and does NOT mark the
    listing as seen, so it stays retryable tomorrow.
    """


def _ssl_context() -> ssl.SSLContext:
    for bundle in _SYSTEM_CA_BUNDLES:
        if os.path.exists(bundle):
            return ssl.create_default_context(cafile=bundle)
    return ssl.create_default_context()


def load_bot_token(env_path: str | Path = ".env") -> Optional[str]:
    """Read TELEGRAM_BOT_TOKEN from a dotenv file, then the process env.

    Only that one key is taken from the file; everything else is ignored.
    """
    token = None
    path = Path(env_path)
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "TELEGRAM_BOT_TOKEN":
                token = value.strip().strip("\"'")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", token)
    if not token or token == "changeme":
        return None
    return token


@dataclass
class TelegramSender:
    """Implements `chc_rental.pipeline.PushSender`."""

    token: str = field(repr=False)
    timeout: float = 15.0
    disable_web_page_preview: bool = False

    def _post(self, method: str, payload: dict) -> dict:
        url = f"{TELEGRAM_API}/bot{self.token}/{method}"
        data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=_ssl_context()) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Telegram puts the useful reason in the body, not the status line.
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                raise TelegramSendError(f"{method} failed with HTTP {exc.code}") from None
            raise TelegramSendError(
                f"{method} refused: {body.get('description', 'unknown error')}"
            ) from None
        except urllib.error.URLError as exc:
            # str(exc) can include the URL, which carries the token.
            raise TelegramSendError(f"{method} could not reach Telegram: {exc.reason}") from None
        if not body.get("ok"):
            raise TelegramSendError(f"{method} refused: {body.get('description', 'unknown error')}")
        return body.get("result", {})

    def can_reach(self, telegram_id: int) -> bool:
        """True if the bot may message this chat. False means they never /started it."""
        try:
            self._post("getChat", {"chat_id": telegram_id})
            return True
        except TelegramSendError:
            return False

    def whoami(self) -> dict:
        return self._post("getMe", {})

    def send(self, *, telegram_id: int, text: str) -> None:
        self._post(
            "sendMessage",
            {
                "chat_id": telegram_id,
                "text": text,
                "disable_web_page_preview": str(self.disable_web_page_preview).lower(),
            },
        )


def build_sender(env_path: str | Path = ".env") -> Optional[TelegramSender]:
    """Return a sender, or None when no usable token is configured."""
    token = load_bot_token(env_path)
    return TelegramSender(token=token) if token else None
