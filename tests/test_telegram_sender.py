"""Telegram sender: token handling, error mapping, and outbound-only shape.

No test here performs network I/O; `_post` is substituted.
"""

from __future__ import annotations

import io
import urllib.error

import pytest

from chc_rental.notify.telegram import (
    TelegramSendError,
    TelegramSender,
    build_sender,
    load_bot_token,
)

FAKE = "8962446657:FAKE-TOKEN-VALUE-DO-NOT-USE-abcdef"


def write_env(tmp_path, value: str):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\nCHC_RENTAL_DB_PATH=data/x.sqlite3\n" f"TELEGRAM_BOT_TOKEN={value}\n",
        encoding="utf-8",
    )
    return path


def test_token_is_read_from_the_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert load_bot_token(write_env(tmp_path, FAKE)) == FAKE


def test_placeholder_token_counts_as_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert load_bot_token(write_env(tmp_path, "changeme")) is None
    assert build_sender(write_env(tmp_path, "changeme")) is None


def test_missing_env_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert load_bot_token(tmp_path / "nope.env") is None


def test_process_environment_overrides_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "9:FROM-ENV")
    assert load_bot_token(write_env(tmp_path, FAKE)) == "9:FROM-ENV"


def test_token_never_appears_in_repr():
    """The retired RentCast adapter leaked its key through a dataclass repr."""
    sender = TelegramSender(token=FAKE)
    assert FAKE not in repr(sender)
    assert "token" not in repr(sender)


def test_send_posts_to_sendmessage_with_the_recipient(monkeypatch):
    calls = []
    sender = TelegramSender(token=FAKE)
    monkeypatch.setattr(
        TelegramSender, "_post", lambda self, m, p: calls.append((m, p)) or {"message_id": 1}
    )
    receipt = sender.send(telegram_id=111, text="hello")
    method, payload = calls[0]
    assert method == "sendMessage"
    assert payload["chat_id"] == 111
    assert payload["text"] == "hello"
    assert receipt.message_id == "1"


def test_a_refused_send_raises_so_the_listing_stays_retryable(monkeypatch):
    """The pipeline only marks a listing seen when send() returns normally."""

    def boom(self, method, payload):
        raise TelegramSendError("sendMessage refused: chat not found")

    monkeypatch.setattr(TelegramSender, "_post", boom)
    with pytest.raises(TelegramSendError):
        TelegramSender(token=FAKE).send(telegram_id=999, text="hi")


def test_http_chat_rejection_is_classified_terminal(monkeypatch):
    def opener(request, timeout, context):
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"ok":false,"description":"bot was blocked"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", opener)
    with pytest.raises(TelegramSendError) as captured:
        TelegramSender(token=FAKE).send(telegram_id=999, text="hi")
    assert captured.value.terminal is True
    assert captured.value.ambiguous is False


def test_http_rate_limit_is_definite_and_exposes_retry_after(monkeypatch):
    def opener(request, timeout, context):
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {},
            io.BytesIO(
                b'{"ok":false,"description":"retry","parameters":{"retry_after":12}}'
            ),
        )

    monkeypatch.setattr("urllib.request.urlopen", opener)
    with pytest.raises(TelegramSendError) as captured:
        TelegramSender(token=FAKE).send(telegram_id=999, text="hi")
    assert captured.value.terminal is False
    assert captured.value.ambiguous is False
    assert captured.value.retry_after == 12


def test_can_reach_is_false_when_the_chat_is_unknown(monkeypatch):
    def boom(self, method, payload):
        raise TelegramSendError("getChat refused: chat not found")

    monkeypatch.setattr(TelegramSender, "_post", boom)
    assert TelegramSender(token=FAKE).can_reach(999) is False


def test_can_reach_is_true_for_a_known_chat(monkeypatch):
    monkeypatch.setattr(TelegramSender, "_post", lambda self, m, p: {"id": 111})
    assert TelegramSender(token=FAKE).can_reach(111) is True


def test_module_never_polls():
    """Outbound only: no receiving endpoint is ever called.

    Checks string *literals* in the compiled module rather than raw text, so the
    docstring explaining the rule doesn't trip it.
    """
    import ast
    from pathlib import Path

    import chc_rental.notify.telegram as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    called = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_post"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            called.add(node.args[0].value)

    assert called, "expected to find the API calls this module makes"
    assert called <= {"sendMessage", "getChat", "getMe"}, f"unexpected endpoint: {called}"
