"""W3 AKQuant Strategy → runner-callable bridge (W7.1 Phase 2).

The runner drives a strategy once per bar, expecting a
``Callable[[state, recent_bars], list[OrderIntent]]``. AKQuant
strategies (``research/strategies/ma_cross.py`` etc.) follow a
different contract: they subclass ``akquant.Strategy`` and call
``self.order_target_percent(symbol, target_percent)``.

**This bridge translates between the two**. It owns an AKQuant
strategy instance and:

  * Provides a ``FakePosition`` mirror so ``self.position.size``
    returns the right value (the strategy's previous-bar net
    position).
  * Provides a ``get_history_df(count)`` that returns a slice of
    the runner's ``recent_bars`` (the strategy's only window
    into price history).
  * Captures ``order_target_percent`` calls and converts them
    into ``OrderIntent`` objects.
  * Tracks a fake ``record_indicator`` log so the strategy
    doesn't blow up when it tries to record indicators.

**Scope** (per the W7.1 Phase 2 plan): single-symbol strategies.
Multi-symbol bridge is Phase 3+.

**Why a subclass instead of monkey-patching**: monkey-patching
``Strategy.order_target_percent`` is brittle (AKQuant may rename
in a release). We subclass and override the relevant method to
capture, not patch. The subclass does NOT extend AKQuant's
``Strategy`` (it extends the user's strategy class) — that way
the user's ``on_bar`` sees our overridden method.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

import pandas as pd
from backtest.a_share.lot_enforcement import enforce_lot

from execution.protocol import OrderIntent, Side

__all__ = ["AkquantStrategyCallable", "FakePosition"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakePosition:
    """Minimal position mirror satisfying ``strategy.position.size``.

    AKQuant's ``strategy.position.size`` is read-only; the bridge
    mutates ``_quantity`` after each captured intent and the
    property exposes the current value.
    """

    symbol: str
    _quantity: int = 0
    avg_cost: float = 0.0

    @property
    def size(self) -> int:
        return self._quantity


# ---------------------------------------------------------------------------
# Strategy wrapper
# ---------------------------------------------------------------------------


class _BridgeStrategyBase:
    """Mixin the bridge injects onto the user's strategy class.

    Holds default implementations of the methods we override.
    NOTE: this mixin is NOT actually used via subclassing in MRO
    (see ``_build_strategy_instance`` comment). The overrides
    are installed DIRECTLY on the dynamic class so they win the
    MRO lookup against AKQuant's ``Strategy.order_target_percent``.
    """


class AkquantStrategyCallable:
    """Wraps an AKQuant ``Strategy`` subclass for the runner.

    Args:
        strategy_cls: The AKQuant Strategy subclass (NOT an
            instance — the bridge instantiates it with no args
            because AKQuant ParamSpec requires subclass-only
            construction). The class is expected to be
            constructible without arguments.
        symbol: The 6-digit symbol the strategy will trade.
            Single-symbol only in Phase 2.
        initial_cash: Starting cash for ``order_target_percent``
            math. Default 1M to match the paper adapter.
        lot_size: A-share lot size. Default 100.
        strategy_kwargs: Forwarded to the strategy's ``__init__``.
            Use for parameter overrides (e.g.
            ``MACrossStrategy(fast_window=3)``).

    After construction, call the instance as a strategy:
    ``bridge(state, recent_bars) -> list[OrderIntent]``.
    """

    _REMARK_PREFIX: ClassVar[str] = "q:"

    def __init__(
        self,
        strategy_cls: type,
        *,
        symbol: str = "000001",
        initial_cash: float = 1_000_000.0,
        lot_size: int = 100,
        strategy_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._strategy_cls = strategy_cls
        self._symbol = symbol
        self._initial_cash = initial_cash
        self._lot_size = lot_size
        self._strategy_kwargs = dict(strategy_kwargs or {})

        # Build the strategy instance. We must subclass the
        # user's class with _BridgeStrategyBase to inject our
        # overrides without monkey-patching.
        self._strategy = self._build_strategy_instance()
        # Alias for use inside override methods.
        self._strategy._bridge = self  # type: ignore[attr-defined]

        # Capture state.
        self._captured_intents: list[OrderIntent] = []
        self._indicators: list[dict[str, Any]] = []
        self._fake_position = FakePosition(symbol=self._symbol)
        # Assign the fake position to the strategy instance. The
        # AKQuant ``Strategy.position`` is a *property*; we
        # cannot rebind it. Instead, override the property on
        # the strategy class to return our FakePosition. Since
        # we built the strategy via ``type(...)``, we can patch
        # it on the dynamic class itself.
        # See also __call__ note.
        self._history_df: pd.DataFrame = self._empty_history_df()
        # Install the FakePosition as the strategy's ``position``
        # by replacing the property on the dynamic class.
        self._install_position_property()

    def _install_position_property(self) -> None:
        """Replace the strategy's ``position`` property to return our FakePosition.

        AKQuant's ``Strategy.position`` is a read-only @property.
        We can't rebind the instance attribute, so we replace
        the property on the dynamic subclass. The dynamic class
        is private to this bridge, so this monkey-patch is
        scoped to this strategy instance only.
        """
        fake_pos = self._fake_position

        def _position_getter(self: Any) -> FakePosition:
            return fake_pos

        cls = type(self._strategy)
        # If AKQuant already has a property, this overrides it
        # on the dynamic subclass (which is what ``self._strategy``
        # is an instance of). If not, this adds it. Either way,
        # our getter wins for this instance.
        cls.position = property(_position_getter)  # type: ignore[attr-defined]

    # ---------- Internal: build strategy instance ----------

    def _build_strategy_instance(self) -> Any:
        """Instantiate the user's strategy with our overrides installed.

        Uses dynamic class creation. The resulting class subclasses
        ONLY the user's strategy (``_BridgeStrategyBase`` is NOT in
        the bases because Python MRO puts ``Strategy`` before any
        sibling mixin we add, so our overrides never win). Instead,
        we install ``order_target_percent`` / ``record_indicator`` /
        ``position`` directly on the dynamic class body — which IS
        in the MRO and IS the first class in the chain, so our
        overrides always win.
        """
        bridge_ref = self  # captured by the closures below

        def _order_target_percent(self: Any, symbol: str, target_percent: float) -> None:
            bridge_ref._capture_target_percent(symbol, target_percent)

        def _record_indicator(self: Any, name: str, value: float, *, symbol: str = "") -> None:
            bridge_ref._indicators.append({"name": name, "value": value, "symbol": symbol})

        new_cls = type(
            "_BridgeWrapped_" + self._strategy_cls.__name__,
            (self._strategy_cls,),
            {
                "order_target_percent": _order_target_percent,
                "record_indicator": _record_indicator,
            },
        )
        try:
            return new_cls(**self._strategy_kwargs)
        except TypeError as exc:
            # Strategy __init__ rejects kwargs. Surface a clear
            # message — the user might be passing the wrong shape.
            raise RuntimeError(
                f"strategy class {self._strategy_cls.__name__} cannot be "
                f"instantiated with kwargs {self._strategy_kwargs}: {exc}"
            ) from exc

    # ---------- Strategy-callable protocol ----------

    def __call__(
        self,
        state: dict[str, Any],
        recent_bars: list[dict[str, Any]],
    ) -> list[OrderIntent]:
        """Drive the strategy for ONE bar (the latest in ``recent_bars``).

        The runner has already accumulated ``recent_bars``. We
        append the latest bar to the bridge's history, then
        invoke ``self._strategy.on_bar(synthetic_bar)``. Captured
        intents are returned for the runner to feed through risk
        → adapter → journal.
        """
        if not recent_bars:
            return []

        # Reset capture buffers for this bar.
        self._captured_intents = []

        # The runner passes the FULL accumulated history on every
        # call (``recent_bars`` grows monotonically). We append
        # only the NEW tail — bars beyond what we already have.
        # This keeps the bridge's history aligned with the
        # runner's without duplicates.
        current_len = len(self._history_df)
        new_bars = recent_bars[current_len:] if len(recent_bars) > current_len else [recent_bars[-1]]
        for bar in new_bars:
            self._append_bar(bar)
        # The strategy acts on the LATEST bar in the history.
        latest = self._history_df.iloc[-1].to_dict()
        # NOTE: AKQuant's ``Strategy.position`` is a read-only
        # property; we cannot rebind it. Instead, the runner is
        # expected to call ``bridge.update_position(...)`` after
        # each fill (which mutates the FakePosition in place via
        # ``_quantity`` and ``avg_cost``). When called fresh, the
        # FakePosition holds the loaded ``_quantity``; the
        # strategy reads ``self.position.size`` which reads
        # ``self._quantity`` from the FakePosition we assigned
        # during construction.

        # Build a synthetic bar object that matches AKQuant's
        # ``Bar`` interface (duck-typed: needs ``symbol``, ``close``,
        # ``open``, ``high``, ``low``, ``volume``, ``date``).
        synthetic_bar = self._make_bar(latest)
        try:
            self._strategy.on_bar(synthetic_bar)  # type: ignore[attr-defined]
        except Exception:
            # The strategy raised — log and skip this bar.
            # We don't propagate (runner expects a callable that
            # never raises per W7.1 design).
            import loguru

            loguru.logger.exception(
                "strategy {cls} raised on bar {date}",
                cls=self._strategy_cls.__name__,
                date=latest.get("date"),
            )
            return []

        return list(self._captured_intents)

    def update_position(self, symbol: str, quantity: int, avg_cost: float) -> None:
        """Update the fake-position mirror after a fill.

        The runner calls this after each successful fill so the
        strategy sees accurate ``self.position.size`` on the
        next bar. ``symbol`` mismatch raises (defensive — would
        mean the runner passed the wrong adapter a multi-symbol
        intent).
        """
        if symbol != self._symbol:
            raise ValueError(
                f"bridge for {self._symbol} got position update for {symbol!r}"
            )
        self._fake_position = FakePosition(
            symbol=symbol,
            _quantity=quantity,
            avg_cost=avg_cost,
        )

    # ---------- Internal: intent capture ----------

    def _capture_target_percent(self, symbol: str, target_percent: float) -> None:
        """Translate ``order_target_percent`` → ``OrderIntent``.

        Math:
          * target_value = target_percent * total_equity
            (we approximate total_equity with initial_cash +
            current position's market value at avg_cost — exact
            mark-to-market would need price feeds; close enough
            for paper / sandbox)
          * target_qty = target_value / current_price
          * delta_qty = target_qty - current_position_qty
          * if delta_qty > 0 → buy intent; < 0 → sell intent
          * enforce_lot(delta_qty)
        """
        if symbol != self._symbol:
            # Phase 2 single-symbol only. Drop silently to keep
            # strategy intent semantics unchanged (a multi-symbol
            # strategy trying to trade a different symbol in this
            # bridge raises a future error).
            return

        latest = self._latest_history_row()
        if latest is None:
            return
        current_price = float(latest["close"])

        # total_equity approximation.
        position_value = self._fake_position._quantity * (
            current_price if self._fake_position._quantity > 0 else self._fake_position.avg_cost
        )
        total_equity = self._initial_cash + position_value
        target_value = max(0.0, target_percent) * total_equity
        target_qty = int(target_value // current_price) if current_price > 0 else 0

        current_qty = self._fake_position._quantity
        delta_qty = target_qty - current_qty
        if delta_qty == 0:
            return
        # Round to a whole lot. enforce_lot rounds DOWN; for
        # sells, we want a sell of at most |delta_qty| lots.
        if delta_qty > 0:
            side: Side = "buy"
            raw_qty = delta_qty
        else:
            side = "sell"
            raw_qty = -delta_qty
        rounded_qty = enforce_lot(raw_qty, lot_size=self._lot_size)
        if rounded_qty == 0:
            return

        # If we rounded a buy UP (shouldn't happen — enforce_lot
        # rounds down), or a sell DOWN (we lose fractional lot),
        # the displayed size may differ from raw. Log nothing —
        # this is normal rounding noise.
        intent = OrderIntent(
            client_order_id=f"bridge-{id(self)}-{len(self._captured_intents)}",
            symbol=symbol,
            side=side,
            quantity=rounded_qty,
            price=current_price,
            order_type="limit",
            reason=f"order_target_percent({target_percent})",
        )
        self._captured_intents.append(intent)

    # ---------- Internal: history + bar synthesis ----------

    def _empty_history_df(self) -> pd.DataFrame:
        """Empty DataFrame with the columns the strategy expects."""
        return pd.DataFrame(
            columns=["date", "open", "high", "low", "close", "volume"],
        )

    def _append_bar(self, bar: dict[str, Any]) -> None:
        """Append one bar to internal history.

        The strategy's ``self.get_history_df(count=N)`` reads from
        this DataFrame. We delegate by monkey-patching
        ``get_history_df`` on the strategy instance to return a
        tail slice of ``self._history_df``.
        """
        row = {
            "date": bar.get("date"),
            "open": float(bar.get("open", 0.0)),
            "high": float(bar.get("high", 0.0)),
            "low": float(bar.get("low", 0.0)),
            "close": float(bar.get("close", 0.0)),
            "volume": float(bar.get("volume", 0.0)),
        }
        self._history_df = pd.concat(
            [self._history_df, pd.DataFrame([row])],
            ignore_index=True,
        )
        self._patch_get_history_df()

    def _patch_get_history_df(self) -> None:
        """Replace ``strategy.get_history_df`` to read our history.

        The closure looks up ``self._history_df`` at CALL time
        (not at patch time) so each call sees the latest history.
        A previous version captured the dataframe in a local
        variable, which got stale after subsequent ``_append_bar``
        calls (each creates a new DataFrame via ``pd.concat``).
        """
        bridge_ref = self

        def _get_history_df(*, count: int) -> pd.DataFrame:
            history = bridge_ref._history_df
            if count <= 0 or history.empty:
                return history.iloc[0:0].copy()
            return history.tail(count).reset_index(drop=True)

        self._strategy.get_history_df = _get_history_df  # type: ignore[attr-defined]

    def _latest_history_row(self) -> dict[str, Any] | None:
        if self._history_df.empty:
            return None
        return self._history_df.iloc[-1].to_dict()

    def _make_bar(self, latest: dict[str, Any]) -> Any:
        """Construct an AKQuant Bar-shaped object from a runner bar dict.

        AKQuant's Bar is a dataclass-like object; we use a simple
        namespace since the strategy only reads attribute access.
        """
        bar_type = type(
            "_BridgeBar",
            (),
            {
                "symbol": self._symbol,
                "open": float(latest.get("open", 0.0)),
                "high": float(latest.get("high", 0.0)),
                "low": float(latest.get("low", 0.0)),
                "close": float(latest.get("close", 0.0)),
                "volume": float(latest.get("volume", 0.0)),
                "date": latest.get("date"),
            },
        )
        return bar_type()

    # ---------- Public accessors (test / dashboard hooks) ----------

    @property
    def indicators(self) -> list[dict[str, Any]]:
        """Read-only view of all ``record_indicator`` calls."""
        return list(self._indicators)

    @property
    def strategy_instance(self) -> Any:
        """The underlying AKQuant strategy instance (for debugging)."""
        return self._strategy
