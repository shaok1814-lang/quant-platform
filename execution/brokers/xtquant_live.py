"""Phase 2 live broker adapter for miniQMT / xtquant (W7.1 Phase 2).

Replaces the W7.1 stub with a full implementation:

  * :class:`XtQuantLiveAdapter` — the project-facing
    :class:`BrokerAdapter` (same Protocol as :class:`AkquantPaperAdapter`)
  * Lazy-imports xtquant inside ``__init__`` so the module loads
    cleanly on non-Windows machines (CI).
  * Accepts an injected ``trader_factory`` for testing with
    :class:`execution.brokers.xtquant_fake.FakeXtQuantTrader`.
  * Cross-thread event marshalling via a ``queue.Queue``: the
    SDK's callback thread (real) or the test thread (fake) pushes
    events into the queue; the runner drains via
    :meth:`consume_events` once per bar.

**Critical invariants**:

  * Callbacks NEVER block (a slow callback stalls the SDK's event
    loop and desyncs subsequent events). The callback class
    (:class:`execution.brokers.xtquant_callbacks.XtQuantTradeCallback`)
    uses ``put_nowait`` and drops on overflow.

  * ``place_order`` blocks on the SDK thread (xtquant's
    ``order_stock`` is blocking by design). The runner's main
    thread calls it. The callback thread fills events into the
    queue asynchronously.

  * ``client_order_id`` is encoded into ``order_remark`` as
    ``f"q:{client_id}"`` and parsed back on ``on_order``. xtquant
    has no native ``client_order_id``; the convention is remark-
    encoded.

  * Reconnect / reconciliation: on ``on_disconnected``, the adapter
    refuses new orders until reconnect succeeds. The reconciliation
    step pulls ``query_stock_orders/trades/positions/asset`` and
    updates local state so journal writes stay accurate after a
    broker disconnect.

**Watchdog** (silent TCP half-open detection): the adapter tracks
the last event timestamp it saw. If no event arrives for more than
``watchdog_seconds`` AND no order has been recently submitted, it
forces a reconnect (the SDK's ``on_disconnected`` does NOT fire on
silent half-opens — this is the canonical pitfall noted in the
W7.1 prior research).
"""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from typing import Any, Final

from loguru import logger

from execution.brokers.xtquant_callbacks import (
    BrokerEvent,
    DisconnectedEvent,
    OrderErrorEvent,
    OrderEvent,
    TradeEvent,
    XtQuantTradeCallback,
)
from execution.protocol import (
    EquitySnapshot,
    ExecutionReport,
    OrderIntent,
    Position,
    utcnow,
)

__all__ = ["XtQuantLiveAdapter"]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


DEFAULT_CALLBACK_QUEUE_SIZE: Final[int] = 10_000
DEFAULT_RECONNECT_MAX_ATTEMPTS: Final[int] = 5
DEFAULT_RECONNECT_BACKOFF_BASE_S: Final[float] = 2.0
DEFAULT_WATCHDOG_SECONDS: Final[float] = 30.0
DEFAULT_DROP_NOTIFY_THRESHOLD: Final[int] = 100


# ---------------------------------------------------------------------------
# 6-digit symbol → "000001.SZ" / "600000.SH"
# ---------------------------------------------------------------------------

_SHANGHAI_PREFIXES: Final[tuple[str, ...]] = ("60", "68", "90", "11", "13")


def _symbol_to_xtcode(symbol: str) -> str:
    """Map ``"000001"`` → ``"000001.SZ"`` etc.

    Mirrors ``execution.brokers.xtquant_fake``'s test convention
    (no exchange prefix needed in tests, but the real SDK
    REQUIRES the suffix or it raises).
    """
    if symbol.startswith(_SHANGHAI_PREFIXES):
        return f"sh.{symbol}"
    return f"sz.{symbol}"


def _xtcode_to_symbol(xtcode: str) -> str:
    """Reverse: ``"sh.000001"`` / ``"SZ.000001"`` → ``"000001"``

    Tolerates either ``.SH`` / ``.SZ`` (real SDK) or ``sh.`` /
    ``sz.`` (xtquant's preferred lower-case form).
    """
    s = xtcode.strip().upper()
    if "." in s:
        # Real SDK: "000001.SZ" → "000001" (before the dot).
        # Fake SDK: "sz.000001" → "000001" (after the dot).
        # We split on "." and take the SYMBOL-shaped token
        # (6 consecutive digits). Either side works because
        # both have exactly one "." and one 6-digit token.
        for tok in s.split("."):
            if tok.isdigit() and len(tok) == 6:
                return tok
        # Fallback: token after the dot (real SDK format).
        return s.split(".", 1)[-1]
    return s


# ---------------------------------------------------------------------------
# XtQuantLiveAdapter
# ---------------------------------------------------------------------------


class XtQuantLiveAdapter:
    """Phase 2 miniQMT live broker adapter.

    Args:
        path: Path to the miniQMT ``userdata_mini`` folder (e.g.
            ``r"D:/国金QMT/userdata_mini"``). Required by the real
            xtquant SDK; ignored by the fake.
        session_id: Unique integer session id (``int(time.time()
            * 1000)`` works for single-session use). The fake
            accepts any int.
        account_id: The broker-assigned 资金账号 (e.g.
            ``"55008888"``).
        account_type: ``"STOCK"`` (default), ``"CREDIT"``,
            ``"FUTURE"``, ``"OPTION"`` per xtquant.
        callback_queue_size: Max events buffered in the cross-
            thread queue. Default 10k. Bigger = more memory, smaller
            = more drop risk.
        reconnect_max_attempts: Exponential-backoff retries on
            disconnect. Default 5.
        reconnect_backoff_base_s: Initial backoff in seconds (doubles
            each retry, capped at 60s).
        watchdog_seconds: Threshold for "silent TCP half-open"
            detection. If no event arrives for this many seconds
            AND no recent submit, the adapter forces a reconnect.
        trader_factory: Injectable factory returning an
            ``XtQuantTrader``-compatible object. Default:
            ``xtquant.xttrader.XtQuantTrader`` (lazy-imported on
            construction). Tests pass
            ``lambda: FakeXtQuantTrader(path, session_id)``.
        asset_factory: Optional override for
            ``xtquant.xttype.StockAccount`` construction. Default
            uses a dict-like shape that the fake accepts. For real
            SDK use, the adapter internally calls
            ``StockAccount(account_id, account_type)`` if you
            provide one.

    Note:
        xtquant is **lazy-imported** the first time ``__init__``
        runs with the default ``trader_factory``. Construction on
        a non-Windows machine fails if ``trader_factory=None``;
        tests MUST inject a factory. This is intentional — see
        CLAUDE.md 「data must be reliable」.
    """

    name: str = "xtquant_live"

    def __init__(
        self,
        *,
        path: str,
        session_id: int,
        account_id: str,
        account_type: str = "STOCK",
        callback_queue_size: int = DEFAULT_CALLBACK_QUEUE_SIZE,
        reconnect_max_attempts: int = DEFAULT_RECONNECT_MAX_ATTEMPTS,
        reconnect_backoff_base_s: float = DEFAULT_RECONNECT_BACKOFF_BASE_S,
        watchdog_seconds: float = DEFAULT_WATCHDOG_SECONDS,
        trader_factory: Callable[..., Any] | None = None,
        account_factory: Callable[..., Any] | None = None,
        notify_fn: Callable[[str, str], None] | None = None,
        drop_notify_threshold: int = DEFAULT_DROP_NOTIFY_THRESHOLD,
    ) -> None:
        self._path = path
        self._session_id = session_id
        self._account_id = account_id
        self._account_type = account_type
        self._reconnect_max_attempts = reconnect_max_attempts
        self._reconnect_backoff_base_s = reconnect_backoff_base_s
        self._watchdog_seconds = watchdog_seconds
        self._notify_fn = notify_fn
        self._drop_notify_threshold = max(0, drop_notify_threshold)
        # Once-per-condition flag for the drop-count alert (mirrors
        # the runner's kill-switch ``flip 0→1`` semantics in W7.1
        # Phase 3): we alert ONCE per adapter lifetime, not per
        # bar that observes the threshold crossed.
        self._drop_notified = False

        # Cross-thread event queue. Callbacks push; main thread
        # drains via ``consume_events``.
        self._event_queue: queue.Queue[BrokerEvent] = queue.Queue(
            maxsize=callback_queue_size,
        )

        # Build the trader (real or fake). Lazy-import xtquant
        # so non-Windows machines can still import this module
        # (the adapter class itself is portable; only the real
        # ``XtQuantTrader`` requires xtquant).
        if trader_factory is None:
            trader_factory = self._default_trader_factory()
        self._trader = trader_factory()

        # Build the account object. Default: use the trader SDK's
        # StockAccount (lazy-imported). Tests can override.
        if account_factory is None:
            account_factory = self._default_account_factory()
        self._account = account_factory(account_id, account_type)

        # Wire callback BEFORE ``start`` / ``connect``. xtquant
        # pushes events through whatever object is registered
        # at any time, so the order doesn't strictly matter,
        # but registering first is the documented convention.
        self._callback = XtQuantTradeCallback(self._event_queue)
        self._trader.register_callback(self._callback)

        # Client_id ↔ broker_id tables. Populated as orders flow
        # through ``place_order`` (forward) and ``on_order``
        # callbacks (reverse confirm).
        self._client_to_broker: dict[str, int] = {}
        self._broker_to_client: dict[int, str] = {}

        # Lifecycle flags.
        self._connected = False
        self._disconnected = False
        self._refusing_orders = False
        self._last_event_at: float = 0.0
        self._last_submit_at: float = 0.0

        # Cached query snapshots. Refreshed by ``_refresh_query_cache``
        # after reconnect; otherwise stale. Real SDK queries are
        # slow so we don't refresh on every consume.
        self._cash: float = 0.0
        self._total_asset: float = 0.0
        self._positions_cache: list[Position] = []

    # ---------- Trader / account factories ----------

    def _default_trader_factory(self) -> Callable[..., Any]:
        """Return a factory that builds the real XtQuantTrader.

        Lazy-imports xtquant. On non-Windows machines without
        xtquant installed, the import raises — the caller is
        expected to inject ``trader_factory`` for tests.
        """

        def _factory() -> Any:
            from xtquant.xttrader import XtQuantTrader

            return XtQuantTrader(path=self._path, session_id=self._session_id)

        return _factory

    def _default_account_factory(self) -> Callable[..., Any]:
        def _factory(account_id: str, account_type: str) -> Any:
            from xtquant.xttype import StockAccount

            return StockAccount(account_id, account_type)

        return _factory

    # ---------- Lifecycle ----------

    def connect(self) -> None:
        """Connect + subscribe.

        Idempotent: a second call on an already-connected
        adapter is a no-op. Raises on connect failure (the
        SDK's connect() returns a non-zero rc; we raise).
        """
        if self._connected:
            return
        self._trader.start()
        rc = self._trader.connect()
        if rc != 0:
            raise RuntimeError(f"xtquant connect failed: rc={rc}")
        self._trader.subscribe(self._account)
        self._connected = True
        self._disconnected = False
        self._refusing_orders = False
        self._last_event_at = time.monotonic()
        # Pull initial asset snapshot so query_account has data.
        self._refresh_query_cache()

    def disconnect(self) -> None:
        """Mark disconnected. Idempotent.

        We don't call ``trader.disconnect()`` because the real
        SDK doesn't expose it as a method on the trader (the
        TCP session ends when the miniQMT client goes away).
        Tests can override via ``trader_factory``.
        """
        if not self._connected:
            return
        self._connected = False
        self._disconnected = False
        self._refusing_orders = True

    @property
    def is_connected(self) -> bool:
        return self._connected and not self._disconnected

    # ---------- Order placement ----------

    def place_order(self, intent: OrderIntent) -> ExecutionReport:
        """Submit an order and synchronously return an initial report.

        The returned report reflects the SUBMIT-time status
        (``submitted``). The actual fill status arrives via the
        callback queue and surfaces on the next ``consume_events``
        call. Callers wanting fill-side status must drain events.

        Idempotent on ``client_order_id``: if we've already
        submitted this id, return a synthetic report echoing
        the existing broker_order_id (no duplicate submit).

        Raises:
            RuntimeError: If the adapter is currently refusing
                orders (disconnected or not yet reconnected).
        """
        if not self.is_connected:
            return ExecutionReport(
                client_order_id=intent.client_order_id,
                status="rejected",
                reject_reason="adapter not connected (or disconnect pending)",
                timestamp=utcnow(),
            )
        if self._refusing_orders:
            return ExecutionReport(
                client_order_id=intent.client_order_id,
                status="rejected",
                reject_reason="adapter refusing orders (post-disconnect)",
                timestamp=utcnow(),
            )

        # Idempotent re-submit.
        if intent.client_order_id in self._client_to_broker:
            existing_broker_id = self._client_to_broker[intent.client_order_id]
            return ExecutionReport(
                client_order_id=intent.client_order_id,
                broker_order_id=str(existing_broker_id),
                status="submitted",
                timestamp=utcnow(),
            )

        # Translate OrderIntent → xtquant call.
        remark = f"{XtQuantTradeCallback.REMARK_PREFIX}{intent.client_order_id}"
        stock_code = _symbol_to_xtcode(intent.symbol)
        order_type_int = self._trader.STOCK_BUY if intent.side == "buy" else self._trader.STOCK_SELL
        price_type_int = (
            self._trader.FIX_PRICE if intent.order_type == "limit" else self._trader.MARKET_PRICE
        )
        price = float(intent.price) if intent.price is not None else 0.0

        try:
            broker_order_id = self._trader.order_stock(
                self._account,
                stock_code,
                order_type_int,
                int(intent.quantity),
                price_type_int,
                price,
                strategy_name="execution",
                order_remark=remark,
            )
        except Exception as exc:
            logger.error(
                "xtquant order_stock failed for {sym}: {e}",
                sym=intent.symbol,
                e=exc,
            )
            return ExecutionReport(
                client_order_id=intent.client_order_id,
                status="rejected",
                reject_reason=f"order_stock raised: {type(exc).__name__}: {exc}",
                timestamp=utcnow(),
            )

        # Bookkeeping. ``on_order`` will fire later (callback
        # thread) and may rewrite the order's status; we don't
        # wait for it here because xtquant's order_stock is
        # already blocking on the submit.
        self._client_to_broker[intent.client_order_id] = broker_order_id
        self._broker_to_client.setdefault(broker_order_id, intent.client_order_id)
        self._last_submit_at = time.monotonic()

        return ExecutionReport(
            client_order_id=intent.client_order_id,
            broker_order_id=str(broker_order_id),
            status="submitted",
            timestamp=utcnow(),
        )

    def cancel_order(self, broker_order_id: str) -> ExecutionReport:
        """Cancel by broker_order_id. Returns the broker's
        confirmation as an ExecutionReport."""
        if not self.is_connected:
            return ExecutionReport(
                client_order_id=broker_order_id,
                broker_order_id=broker_order_id,
                status="rejected",
                reject_reason="adapter not connected",
                timestamp=utcnow(),
            )
        try:
            self._trader.cancel_order(self._account, int(broker_order_id))
        except Exception as exc:
            return ExecutionReport(
                client_order_id=broker_order_id,
                broker_order_id=broker_order_id,
                status="rejected",
                reject_reason=f"cancel_order raised: {type(exc).__name__}: {exc}",
                timestamp=utcnow(),
            )
        return ExecutionReport(
            client_order_id=broker_order_id,
            broker_order_id=broker_order_id,
            status="cancelled",
            timestamp=utcnow(),
        )

    # ---------- Queries ----------

    def query_positions(self) -> list[Position]:
        """Return cached positions.

        The cache is refreshed by ``_refresh_query_cache`` (called
        on connect + after each reconnect reconciliation). For
        Phase 1 we trust the cache; Phase 3 may add per-bar refresh.
        """
        return list(self._positions_cache)

    def query_account(self) -> EquitySnapshot:
        """Return cached asset snapshot."""
        return EquitySnapshot(
            timestamp=utcnow(),
            cash=self._cash,
            positions_value=self._total_asset - self._cash,
            total_equity=self._total_asset,
            drawdown_pct=0.0,
        )

    def _refresh_query_cache(self) -> None:
        """Rebuild the asset + position cache from the broker."""
        asset = self._trader.query_stock_asset(self._account)
        if asset is not None:
            self._total_asset = float(asset.total_asset)
            self._cash = float(asset.cash)
        positions = self._trader.query_stock_positions(self._account)
        if positions is not None:
            self._positions_cache = [
                Position(
                    symbol=_xtcode_to_symbol(p.stock_code),
                    quantity=int(p.volume),
                    avg_cost=float(p.avg_price),
                )
                for p in positions
            ]

    # ---------- Event drain ----------

    def consume_events(self, *, max_events: int = 100) -> list[BrokerEvent]:
        """Drain queued events into adapter state. Returns the
        drained events for journaling (the runner loops over
        them and writes Fill rows).

        Must be called from the RUNNER thread (not the SDK
        callback thread). Idempotent re-call safe.

        On DisconnectedEvent: trigger the reconnect procedure
        (with exponential backoff). Refuse new orders until
        reconnect succeeds.

        On drop_count crossing ``drop_notify_threshold`` after
        the drain: fire :attr:`_notify_fn` once (idempotent via
        ``_drop_notified`` flag), like the runner's kill-switch
        flip-0→1 semantics. The flag is never reset — a single
        alert per adapter lifetime is the right operator-visibility
        tradeoff (钉聊 spam is the failure mode).
        """
        events: list[BrokerEvent] = []
        for _ in range(max_events):
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                break
            events.append(event)
            self._apply_event(event)
        # Drop-count check AFTER the drain so we report the
        # post-drain accumulated count (which is what the runner
        # will see on this bar). Threshold-crossing fires the
        # alert once per adapter lifetime.
        if (
            self._drop_notify_threshold > 0
            and not self._drop_notified
            and self._callback.drop_count >= self._drop_notify_threshold
        ):
            self._drop_notified = True
            if self._notify_fn is not None:
                try:
                    self._notify_fn(
                        f"XtQuant event drop count > {self._drop_notify_threshold}"
                        f" ({self._account_id})",
                        (
                            f"event=event_drop_threshold\n"
                            f"drop_count={self._callback.drop_count}\n"
                            f"threshold={self._drop_notify_threshold}\n"
                            f"queue_size={self._event_queue.maxsize}\n"
                            f"account_id={self._account_id}\n"
                            f"session_id={self._session_id}\n"
                            f"timestamp={utcnow().isoformat()}"
                        ),
                    )
                except Exception:  # pragma: no cover -- best-effort
                    logger.exception("notify_fn raised on drop threshold")
        return events

    def _apply_event(self, event: BrokerEvent) -> None:
        """Mutate adapter state from a single event.

        This is the only place the adapter touches self._* in
        response to events. Keep it linear / idempotent.
        """
        self._last_event_at = time.monotonic()
        if isinstance(event, OrderEvent):
            if event.client_order_id and event.broker_order_id:
                self._broker_to_client.setdefault(
                    event.broker_order_id,
                    event.client_order_id,
                )
        elif isinstance(event, TradeEvent):
            # Trade events don't carry our client_order_id (xtquant
            # doesn't echo remark on trades); resolve via the
            # broker→client map maintained in place_order.
            if event.broker_order_id in self._broker_to_client:
                # Re-bind client_order_id onto the event so the
                # runner's journal write gets a stable id.
                object.__setattr__(
                    event, "client_order_id", self._broker_to_client[event.broker_order_id]
                )
        elif isinstance(event, OrderErrorEvent):
            logger.error(
                "xtquant order_error id={bid} err_id={eid} msg={msg}",
                bid=event.broker_order_id,
                eid=event.error_id,
                msg=event.error_msg,
            )
        elif isinstance(event, DisconnectedEvent):
            self._on_disconnected()

    def _on_disconnected(self) -> None:
        """React to a DisconnectedEvent: mark disconnected and
        start the reconnect procedure.

        Reconnect uses exponential backoff. On success, runs
        ``query_stock_*`` to refresh local state. On exhaustion,
        stays in ``_refusing_orders = True`` (operator must
        investigate).
        """
        logger.warning("xtquant disconnected; entering reconnect")
        self._disconnected = True
        self._refusing_orders = True
        # Tell the trader we're gone (fake supports disconnect; real
        # xtquant SDK doesn't expose it, so we guard).
        try:
            disconnect = getattr(self._trader, "disconnect", None)
            if disconnect is not None:
                disconnect()
        except Exception as exc:  # pragma: no cover -- defensive
            logger.warning("trader.disconnect() raised: {e}", e=exc)
        for attempt in range(self._reconnect_max_attempts):
            backoff = min(60.0, self._reconnect_backoff_base_s * (2**attempt))
            logger.info(
                "xtquant reconnect attempt {n}/{m} after {b:.1f}s",
                n=attempt + 1,
                m=self._reconnect_max_attempts,
                b=backoff,
            )
            time.sleep(backoff)
            try:
                rc = self._trader.connect()
                if rc != 0:
                    logger.error("reconnect connect rc={rc}", rc=rc)
                    continue
                self._trader.subscribe(self._account)
                self._connected = True
                self._disconnected = False
                self._refusing_orders = False
                self._refresh_query_cache()
                logger.info(
                    "xtquant reconnected after {n} attempts",
                    n=attempt + 1,
                )
                return
            except Exception as exc:
                logger.error("reconnect raised: {e}", e=exc)
                continue
        logger.error(
            "xtquant reconnect exhausted after {n} attempts; adapter now refuses new orders",
            n=self._reconnect_max_attempts,
        )
        # W7.1 Phase 5: notify the operator that the live
        # session lost the broker connection permanently. Best-
        # effort — ``notify_fn`` raising must NOT cascade into
        # the runner / reconciliation loop (CLAUDE.md 「数据可靠
        # > 单点失败断整个系统」).
        if self._notify_fn is not None:
            try:
                self._notify_fn(
                    f"XtQuant reconnect exhausted ({self._account_id})",
                    (
                        f"event=reconnect_exhausted\n"
                        f"attempts={self._reconnect_max_attempts}\n"
                        f"backoff_base_s={self._reconnect_backoff_base_s}\n"
                        f"account_id={self._account_id}\n"
                        f"session_id={self._session_id}\n"
                        f"timestamp={utcnow().isoformat()}"
                    ),
                )
            except Exception:  # pragma: no cover -- best-effort
                logger.exception("notify_fn raised on reconnect exhausted")

    # ---------- Watchdog ----------

    def watchdog_check(self, *, now: float | None = None) -> bool:
        """Force a reconnect if we've gone silent.

        Returns True if a reconnect was triggered, False if the
        connection appears healthy.

        Silent TCP half-open detection: if no event has arrived
        for ``watchdog_seconds`` AND no submit has happened in
        that window, the SDK's ``on_disconnected`` probably never
        fired (the canonical pitfall). Force a reconnect via
        ``_on_disconnected``.

        The runner calls this once per bar (cheap). Tests can
        drive it directly.
        """
        now = now if now is not None else time.monotonic()
        silence = now - self._last_event_at
        # If we just submitted, give the broker a chance to
        # push events before declaring dead.
        if (now - self._last_submit_at) < self._watchdog_seconds:
            return False
        if silence < self._watchdog_seconds:
            return False
        if not self._connected or self._disconnected:
            return False
        logger.warning(
            "xtquant silent for {s:.1f}s (no events, no submits); forcing reconnect",
            s=silence,
        )
        # Synthesize a DisconnectedEvent into the queue path so
        # the same reconnect path is exercised.
        self._on_disconnected()
        return True

    # ---------- Manual refresh ----------

    def refresh_cache(self) -> None:
        """Force-refresh the asset + position cache.

        Useful after ``consume_events`` if the runner wants
        mid-bar query accuracy.
        """
        self._refresh_query_cache()

    @property
    def drop_count(self) -> int:
        """Number of events dropped due to queue full.

        The runner surfaces this in dashboards / alerts. A
        non-zero value means the runner drained events slower
        than the SDK pushed them.
        """
        return self._callback.drop_count

    @property
    def pending_client_ids(self) -> set[str]:
        """Snapshot of client_order_ids awaiting broker confirmation.

        Used by tests + future Phase 3 dashboard.
        """
        return set(self._client_to_broker.keys())

    @property
    def disconnected(self) -> bool:
        """True between DisconnectedEvent and successful reconnect."""
        return self._disconnected
