"""DingTalk webhook notifier (W6.1.3).

Sends markdown-formatted alerts to a DingTalk custom robot when
``DINGTALK_WEBHOOK_URL`` is set in the environment. Default
**inactive**: if the env var is missing, ``ding()`` returns a
``NotifyResult(sent=False, error="webhook not configured")`` without
raising. This keeps the project runnable on machines without 钉钉
configured (dev laptops, CI runners), per CLAUDE.md "个人项目不要
接 PagerDuty" + W6.1 do-not-fail-on-missing-config.

Wiring pattern::

    from ops import notify
    if not notify.is_enabled():
        logger.warning("钉钉 not configured, would have sent: {t}", t=title)
    notify.ding("OHLCV quality failure (000001)", report.to_markdown())

Activation:

  1. In DingTalk group, 群设置 → 智能群助手 → 添加机器人 → 自定义
     → 取 ``Webhook`` URL (looks like ``https://oapi.dingtalk.com/robot/send?access_token=...``).
  2. If you enabled "加签" security, also note the secret string.
  3. Set both env vars:

         export DINGTALK_WEBHOOK_URL="https://..."
         # optional, only if you enabled signing:
         export DINGTALK_SECRET="SEC..."

  4. Restart the scheduler.

The signing protocol (HMAC-SHA256 timestamp + sign) is implemented
when ``DINGTALK_SECRET`` is set, omitted otherwise (DingTalk accepts
un-signed webhooks if the security setting is off).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import requests
from loguru import logger

__all__ = [
    "ENV_SECRET",
    "ENV_WEBHOOK",
    "NotifyResult",
    "ding",
    "is_enabled",
]


ENV_WEBHOOK: str = "DINGTALK_WEBHOOK_URL"
ENV_SECRET: str = "DINGTALK_SECRET"

# 钉钉 custom robots cap message size at 20KB; we cap at 16KB to
# leave headroom for 钉Talk's own wrapper. Truncation appends a
# ``[truncated]`` marker so recipients know the body is incomplete.
MAX_MSG_BYTES: int = 16 * 1024

# Bound on request timeout. 钉Talk's webhook endpoint usually
# responds in < 2s; 10s leaves room for transient slow networks
# without holding the scheduler forever.
_REQUEST_TIMEOUT_S: float = 10.0


@dataclass(frozen=True)
class NotifyResult:
    """Outcome of a single :func:`ding` call.

    Attributes:
        sent: ``True`` if the webhook returned HTTP 2xx.
        status_code: HTTP status, or ``None`` if the call was a
            no-op (webhook not configured) or never reached the
            network layer.
        error: Human-readable error description for failed /
            non-sent calls. ``None`` on success.
    """

    sent: bool
    status_code: int | None = None
    error: str | None = None


def is_enabled() -> bool:
    """``True`` iff :data:`ENV_WEBHOOK` is set in the environment.

    Cheap O(1) check; safe to call on every ingest tick.
    """
    return bool(os.environ.get(ENV_WEBHOOK))


def _compute_sign(secret: str) -> tuple[str, str]:
    """Compute (timestamp, sign) per DingTalk's HMAC-SHA256 spec.

    Returns:
        Pair ``(timestamp_str, sign_str)`` suitable for the
        ``timestamp`` and ``sign`` query params on the webhook URL.
    """
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return timestamp, sign


def _build_url(webhook: str) -> str:
    """Augment ``webhook`` with signed timestamp/sign if a secret
    is set; return unchanged otherwise."""
    secret = os.environ.get(ENV_SECRET)
    if not secret:
        return webhook
    ts, sign = _compute_sign(secret)
    sep = "&" if "?" in webhook else "?"
    return f"{webhook}{sep}timestamp={ts}&sign={sign}"


def _truncate(body: str) -> str:
    """Truncate ``body`` to :data:`MAX_MSG_BYTES` (UTF-8 byte length)
    and append a ``[truncated]`` marker so recipients know."""
    encoded = body.encode("utf-8")
    if len(encoded) <= MAX_MSG_BYTES:
        return body
    truncated = encoded[:MAX_MSG_BYTES].decode("utf-8", errors="ignore")
    return truncated + "\n[truncated]"


def ding(title: str, body: str, *, at_mobiles: list[str] | None = None) -> NotifyResult:
    """Send a markdown message to the configured DingTalk webhook.

    Args:
        title: One-line title (used for markdown ``atMobiles``,
            excluded from body unless prefixed manually).
        body: Markdown body. Truncated to :data:`MAX_MSG_BYTES`
            if needed.
        at_mobiles: Optional list of mobile numbers to @mention
            in the alert. Pass empty / ``None`` to mention nobody.

    Returns:
        :class:`NotifyResult`. NEVER raises (network errors are
        caught and surfaced via ``error`` so the caller's ingest
        loop is not aborted by a transient 钉聊 outage).

    Side effect:
        Logs the outcome at INFO (sent) / WARNING (skipped) /
        ERROR (failed). Loguru handles alerting when 钉聊 is
        offline.
    """
    webhook = os.environ.get(ENV_WEBHOOK)
    if not webhook:
        logger.warning(
            "dingtalk webhook not configured (env {env} missing); skipping alert title={t!r}",
            env=ENV_WEBHOOK,
            t=title,
        )
        return NotifyResult(sent=False, error="webhook not configured")

    url = _build_url(webhook)
    body_trunc = _truncate(body)
    payload: dict[str, Any] = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": f"**{title}**\n\n{body_trunc}"},
    }
    if at_mobiles:
        payload["at"] = {"atMobiles": at_mobiles, "isAtAll": False}

    try:
        resp = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT_S)
    except requests.RequestException as exc:
        logger.error(
            "dingtalk post failed for title={t!r}: {e}",
            t=title,
            e=exc,
        )
        return NotifyResult(sent=False, error=str(exc))
    if not resp.ok:
        logger.error(
            "dingtalk non-2xx for title={t!r}: status={s} body={b}",
            t=title,
            s=resp.status_code,
            b=resp.text[:200],
        )
        return NotifyResult(
            sent=False,
            status_code=resp.status_code,
            error=f"HTTP {resp.status_code}",
        )
    logger.info("dingtalk sent title={t!r} status={s}", t=title, s=resp.status_code)
    return NotifyResult(sent=True, status_code=resp.status_code)
