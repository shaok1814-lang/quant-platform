"""Tests for ``ops.notify`` (W6.1.3).

Validates:

  * ``is_enabled()`` reflects the env var.
  * ``ding()`` with no webhook configured is a no-op (returns
    ``NotifyResult(sent=False, error="webhook not configured")``)
    and DOES NOT raise.
  * ``ding()`` with a webhook configured POSTs the right
    markdown JSON to the right URL.
  * Signed URLs include ``timestamp=`` + ``sign=`` query params
    when ``DINGTALK_SECRET`` is set.
  * Network errors / non-2xx responses are surfaced as
    ``NotifyResult(sent=False)`` without raising.
  * Body truncation at :data:`MAX_MSG_BYTES` bounds.

All tests use ``unittest.mock.patch`` on ``ops.notify.requests.post``
(or the constant ``requests`` module). No real network calls.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops import notify  # noqa: E402
from ops.notify import ENV_SECRET, ENV_WEBHOOK, NotifyResult, ding, is_enabled  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear 钉聊 env vars before each test so test cases don't
    leak into each other."""
    monkeypatch.delenv(ENV_WEBHOOK, raising=False)
    monkeypatch.delenv(ENV_SECRET, raising=False)


def test_is_enabled_false_without_env() -> None:
    """Without ``DINGTALK_WEBHOOK_URL``, ``is_enabled()`` is False."""
    assert is_enabled() is False


def test_is_enabled_true_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the env var set, ``is_enabled()`` is True."""
    monkeypatch.setenv(ENV_WEBHOOK, "https://example.com/hook")
    assert is_enabled() is True


def test_ding_no_webhook_returns_unconfigured_result() -> None:
    """``ding()`` without env var: no-op, returns sent=False,
    does NOT raise."""
    result = ding("title", "body")
    assert isinstance(result, NotifyResult)
    assert result.sent is False
    assert result.status_code is None
    assert result.error == "webhook not configured"


def test_ding_no_webhook_does_not_call_requests() -> None:
    """``ding()`` without env var must NOT touch the network."""
    with patch.object(notify.requests, "post") as mock_post:
        ding("title", "body")
        mock_post.assert_not_called()


def test_ding_sends_markdown_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ding()`` with env var POSTs the expected markdown JSON to
    the webhook URL and returns sent=True."""
    monkeypatch.setenv(ENV_WEBHOOK, "https://example.com/hook")
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    with patch.object(notify.requests, "post", return_value=mock_resp) as mock_post:
        result = ding("OHLCV fail", "details here")
    assert result.sent is True
    assert result.status_code == 200
    mock_post.assert_called_once()
    call = mock_post.call_args
    # URL is the first positional arg.
    assert call.args[0] == "https://example.com/hook"
    payload = call.kwargs["json"]
    assert payload["msgtype"] == "markdown"
    assert "OHLCV fail" in payload["markdown"]["title"]
    assert "OHLCV fail" in payload["markdown"]["text"]
    assert "details here" in payload["markdown"]["text"]


def test_ding_with_at_mobiles(monkeypatch: pytest.MonkeyPatch) -> None:
    """``at_mobiles`` propagates into the payload's ``at`` block."""
    monkeypatch.setenv(ENV_WEBHOOK, "https://example.com/hook")
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    with patch.object(notify.requests, "post", return_value=mock_resp) as mock_post:
        ding("ping", "body", at_mobiles=["13800000000"])
    payload = mock_post.call_args.kwargs["json"]
    assert payload["at"] == {"atMobiles": ["13800000000"], "isAtAll": False}


def test_ding_no_at_mobiles_omits_at_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``at_mobiles``, the ``at`` block is absent
    (so a webhook without ``isAtAll`` doesn't accidentally
    ping everyone)."""
    monkeypatch.setenv(ENV_WEBHOOK, "https://example.com/hook")
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    with patch.object(notify.requests, "post", return_value=mock_resp) as mock_post:
        ding("ping", "body")
    payload = mock_post.call_args.kwargs["json"]
    assert "at" not in payload


def test_ding_signed_url_with_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``DINGTALK_SECRET`` is set, the POST URL gains
    ``timestamp=`` and ``sign=`` query params (signature is
    HMAC-SHA256 of ``timestamp\\nsecret`` per 钉聊 spec)."""
    monkeypatch.setenv(ENV_WEBHOOK, "https://example.com/hook")
    monkeypatch.setenv(ENV_SECRET, "SEC123")
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    with patch.object(notify.requests, "post", return_value=mock_resp) as mock_post:
        ding("t", "b")
    url = mock_post.call_args.args[0]
    assert "timestamp=" in url
    assert "sign=" in url


def test_ding_network_error_surfaces_as_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``requests.RequestException`` is caught and surfaced
    via ``NotifyResult(sent=False, error=...)``; the caller
    never sees the exception (would break the ingest loop)."""
    import requests

    monkeypatch.setenv(ENV_WEBHOOK, "https://example.com/hook")
    with patch.object(notify.requests, "post", side_effect=requests.ConnectionError("boom")):
        result = ding("t", "b")
    assert result.sent is False
    assert result.status_code is None
    assert "boom" in (result.error or "")


def test_ding_non_2xx_response_surfaces_as_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-2xx HTTP response (钉聊 returns 200 even for "errcode"-
    wrapped errors usually, but a network layer 4xx/5xx is possible
    e.g. on a misconfigured webhook) is caught and surfaces via
    ``sent=False``."""
    monkeypatch.setenv(ENV_WEBHOOK, "https://example.com/hook")
    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 500
    mock_resp.text = "internal error"
    with patch.object(notify.requests, "post", return_value=mock_resp):
        result = ding("t", "b")
    assert result.sent is False
    assert result.status_code == 500
    assert "500" in (result.error or "")


def test_ding_truncates_long_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Body longer than :data:`MAX_MSG_BYTES` is truncated, with
    a ``[truncated]`` marker so the recipient knows."""
    monkeypatch.setenv(ENV_WEBHOOK, "https://example.com/hook")
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    long_body = "x" * (notify.MAX_MSG_BYTES + 5000)
    with patch.object(notify.requests, "post", return_value=mock_resp) as mock_post:
        ding("t", long_body)
    payload = mock_post.call_args.kwargs["json"]
    text = payload["markdown"]["text"]
    assert "[truncated]" in text
    assert (
        len(text.encode("utf-8")) <= notify.MAX_MSG_BYTES + 50
    )  # 50 = " [truncated]" marker + title overhead


def test_ding_short_body_not_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short body is unchanged (no truncation marker)."""
    monkeypatch.setenv(ENV_WEBHOOK, "https://example.com/hook")
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    with patch.object(notify.requests, "post", return_value=mock_resp) as mock_post:
        ding("t", "hello")
    payload = mock_post.call_args.kwargs["json"]
    assert "[truncated]" not in payload["markdown"]["text"]
