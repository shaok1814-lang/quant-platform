"""W7.1 Phase 4 multi-symbol AKQuant Strategy → runner bridge.

The runner drives a strategy once per bar, expecting a callable
returning a list of OrderIntents. AKQuant strategies follow a
different contract: they subclass ``akquant.Strategy`` and call
``self.order_target_percent(symbol=, target_percent=)``.

**Phase 4 update**: the bridge now supports BOTH single-symbol
(backward compat) AND multi-symbol mode. The constructor ``symbol``
arg defaults to ``None`` (multi-symbol). Pass a string to opt
into single-symbol mode (kept for W7.1 / Phase 2 callers).

Multi-symbol mode design:

  * Per-symbol state: ``_fake_positions: dict[symbol, FakePosition]``,
    ``_history: dict[symbol, pd.DataFrame]``.
  * ``__call__(state, bars_per_symbol: dict[symbol, list[Bar]])``
    iterates per-symbol, swaps ``self._active_symbol`` before each
    ``on_bar()`` call so ``self.position.size`` reads the right
    symbol's quantity.
  * ``self.get_history_df(count, symbol=...)`` is patched to look
    up the right symbol's history DataFrame.
  * ``order_target_percent(symbol=X, percent=Y)`` carries the
    symbol itself, so the bridge override just routes by ``symbol``
    regardless of the active symbol.

**Why override on the dynamic class, not patch instance**: AKQuant
strategies inherit from ``akquant.Strategy`` which has a class-level
``position`` property. We replace that property on the dynamic
subclass so ``self.position`` (inside the user's on_bar) hits our
``_position_getter`` closure.
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

    One :class:`FakePosition` PER SYMBOL (stored in
    ``_fake_positions[symbol]``). The dynamic-class ``position``
    property reads from this dict, keyed by ``self._active_symbol``
    (set by the bridge before each ``on_bar()`` call).
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
    """Unused mixin kept for historical reference.

    The overrides are installed DIRECTLY on the dynamic class
    (see ``_build_strategy_instance``). Listing them on a
    separate mixin would NOT work: Python MRO puts ``Strategy``
    before any sibling mixin we add, so ``Strategy.order_target_percent``
    would win every time. Kept here as documentation.
    """


class AkquantStrategyCallable:
    """Wraps an AKQuant ``Strategy`` subclass for the runner.

    Args:
        strategy_cls: The AKQuant Strategy subclass. Must be
            constructible without positional args (or pass
            ``strategy_kwargs``).
        symbol: 6-digit symbol — single-symbol mode. ``None`` (default)
            → multi-symbol mode (the bridge accepts intents for any
            symbol).
        initial_cash: Starting cash for ``order_target_percent`` math.
        lot_size: A-share lot size. Default 100.
        strategy_kwargs: Forwarded to the strategy's ``__init__``.

    After construction, call the instance as a strategy:
    ``bridge(state, bars_per_symbol) -> list[OrderIntent]``.
    """

    _REMARK_PREFIX: ClassVar[str] = "q:"

    def __init__(
        self,
        strategy_cls: type,
        *,
        symbol: str | None = None,
        initial_cash: float = 1_000_000.0,
        lot_size: int = 100,
        strategy_kwargs: dict[str, Any] | None = None,
    ) -> None:
        if symbol is not None and not isinstance(symbol, str):
            raise TypeError(f"symbol must be str or None, got {type(symbol).__name__}")
        self._strategy_cls = strategy_cls
        self._fixed_symbol = symbol  # None = multi-symbol; str = single-symbol
        self._initial_cash = initial_cash
        self._lot_size = lot_size
        self._strategy_kwargs = dict(strategy_kwargs or {})

        # Build the strategy instance. We install overrides DIRECTLY
        # on the dynamic class body so they win the MRO.
        self._strategy = self._build_strategy_instance()
        # Alias for use inside override methods.
        self._strategy._bridge = self

        # Capture state (reset per __call__).
        self._captured_intents: list[OrderIntent] = []
        self._indicators: list[dict[str, Any]] = []

        # Multi-symbol state: per-symbol FakePosition + per-symbol history.
        self._fake_positions: dict[str, FakePosition] = {}
        self._history: dict[str, pd.DataFrame] = {}
        # Tracks "current symbol" during __call__ — drives the
        # position property lookup.
        self._active_symbol: str | None = None

        # Install the position property on the dynamic class.
        self._install_position_property()

    # ---------- Internal: build strategy instance ----------

    def _build_strategy_instance(self) -> Any:
        """Instantiate the user's strategy with our overrides installed.

        Uses dynamic class creation. The resulting class subclasses
        ONLY the user's strategy. ``_BridgeStrategyBase`` is NOT in
        the bases because Python MRO puts ``Strategy`` before any
        sibling mixin we add, so our overrides never win. Instead,
        we install ``order_target_percent`` / ``record_indicator``
        directly on the dynamic class body — which IS in the MRO
        and IS the first class in the chain, so our overrides
        always win.
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
            raise RuntimeError(
                f"strategy class {self._strategy_cls.__name__} cannot be "
                f"instantiated with kwargs {self._strategy_kwargs}: {exc}"
            ) from exc

    def _install_position_property(self) -> None:
        """Replace the strategy's ``position`` property to return the
        ACTIVE symbol's FakePosition.

        The closure reads ``bridge_ref._active_symbol`` (set by
        ``__call__`` right before each ``on_bar()``). When unset
        (e.g. test calls ``on_bar`` directly), returns a defensive
        empty FakePosition.
        """
        bridge_ref = self

        def _position_getter(self_strategy: Any) -> FakePosition:
            sym = bridge_ref._active_symbol
            if sym is None:
                # Defensive: tests calling on_bar without going
                # through __call__ see an empty position. Bridge-
                # driven paths always set _active_symbol first.
                return FakePosition(symbol="")
            return bridge_ref._get_or_create_position(sym)

        cls = type(self._strategy)
        cls.position = property(_position_getter)

    def _get_or_create_position(self, symbol: str) -> FakePosition:
        """Lazy-init the FakePosition for ``symbol``."""
        if symbol not in self._fake_positions:
            self._fake_positions[symbol] = FakePosition(symbol=symbol)
        return self._fake_positions[symbol]

    # ---------- Strategy-callable protocol ----------

    def __call__(
        self,
        state: dict[str, Any],
        recent_bars: dict[str, list[dict[str, Any]]] | list[dict[str, Any]],
    ) -> list[OrderIntent]:
        """Drive the strategy for ONE bar across ALL symbols.

        Two-pass execution:

        1. **History pre-load**: append each symbol's new bars to
           its history. After this pass, ``get_history_df(symbol=X)``
           works for any X the strategy might query — important for
           cross-symbol strategies that emit ``order_target_percent``
           for symbol Y while ``on_bar(X_bar)`` is running.
        2. **on_bar fan-out**: iterate per-symbol, swap
           ``self._active_symbol``, call ``on_bar`` with this
           symbol's latest bar.

        Captured intents (which carry their own ``symbol``) are
        aggregated across symbols and returned to the runner.

        ``recent_bars`` is ``{symbol: [bar, bar, ...]}``. Each
        symbol's list is monotonically-growing across calls (the
        runner passes the full accumulated history).

        Backward compat: a plain ``list[Bar]`` is auto-wrapped into
        ``{self._fixed_symbol or "_default_": recent_bars}`` so
        tests that pre-date multi-symbol mode continue to work.
        """
        if isinstance(recent_bars, list):
            # Backward compat: a flat list is single-symbol. Use
            # the bar's ``symbol`` attribute if present (W7.1 +
            # Phase 2 runner passed ``symbol`` per bar), else fall
            # back to ``self._fixed_symbol`` (multi-symbol bridge
            # constructed with a fixed symbol), else ``"_default_"``.
            sample_symbol = ""
            if recent_bars and isinstance(recent_bars[0], dict):
                sample_symbol = str(recent_bars[0].get("symbol", ""))
            wrapped_symbol = sample_symbol or self._fixed_symbol or "_default_"
            recent_bars = {wrapped_symbol: recent_bars}

        if not recent_bars:
            return []

        # Reset capture buffers for this bar.
        self._captured_intents = []

        # Pass 1: pre-load all symbols' histories. We don't touch
        # _active_symbol here — get_history_df looks up by symbol
        # arg or falls back to None (returns empty).
        for symbol, bars in recent_bars.items():
            if not bars:
                continue
            self._append_bars(symbol, bars)

        # Pass 2: drive on_bar per symbol. Set _active_symbol so the
        # position property returns this symbol's FakePosition.
        for symbol, bars in recent_bars.items():
            if not bars:
                continue
            self._active_symbol = symbol
            latest = bars[-1]
            synthetic_bar = self._make_bar(symbol, latest)
            try:
                self._strategy.on_bar(synthetic_bar)
            except Exception:
                import loguru

                loguru.logger.exception(
                    "strategy {cls} raised on bar {date} symbol={sym}",
                    cls=self._strategy_cls.__name__,
                    date=latest.get("date"),
                    sym=symbol,
                )

        self._active_symbol = None  # reset (defensive — next call needs fresh state)
        return list(self._captured_intents)

    def update_position(self, symbol: str, quantity: int, avg_cost: float) -> None:
        """Update the fake-position mirror after a fill.

        Single-symbol mode (``_fixed_symbol is not None``) validates
        the symbol matches. Multi-symbol mode accepts any symbol.

        The runner calls this after each successful fill so the
        strategy sees accurate ``self.position.size`` on the next
        bar.
        """
        if self._fixed_symbol is not None and symbol != self._fixed_symbol:
            raise ValueError(f"bridge for {self._fixed_symbol} got position update for {symbol!r}")
        self._fake_positions[symbol] = FakePosition(
            symbol=symbol,
            _quantity=quantity,
            avg_cost=avg_cost,
        )

    # ---------- Internal: intent capture ----------

    def _capture_target_percent(self, symbol: str, target_percent: float) -> None:
        """Translate ``order_target_percent`` → ``OrderIntent``.

        Math (per-symbol):
          * target_value = target_percent * total_equity
            (we approximate total_equity with initial_cash +
            sum of all positions' market value at avg_cost — exact
            mark-to-market would need price feeds)
          * target_qty = target_value / current_price (per symbol)
          * delta_qty = target_qty - current_position_qty
          * if delta_qty > 0 → buy intent; < 0 → sell intent
          * enforce_lot(delta_qty)
        """
        # Single-symbol mode: drop any symbol mismatch (backward compat).
        if self._fixed_symbol is not None and symbol != self._fixed_symbol:
            return

        hist = self._history.get(symbol)
        if hist is None or hist.empty:
            return
        current_price = float(hist.iloc[-1]["close"])

        pos = self._get_or_create_position(symbol)
        # total_equity approximation: cash + sum of all positions
        # valued at avg_cost (not the most accurate, but
        # deterministic for paper testing).
        position_value = sum(
            self._get_or_create_position(s)._quantity
            * (
                self._history[s]["close"].iloc[-1]
                if not self._history[s].empty
                else self._get_or_create_position(s).avg_cost
            )
            for s in self._fake_positions
        )
        total_equity = self._initial_cash + position_value
        target_value = max(0.0, target_percent) * total_equity
        target_qty = int(target_value // current_price) if current_price > 0 else 0

        current_qty = pos._quantity
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

    def _append_bars(self, symbol: str, bars: list[dict[str, Any]]) -> None:
        """Append NEW bars to this symbol's history (incremental sync).

        The runner passes the FULL accumulated history on every
        call. We append only the NEW tail — bars beyond what we
        already have for this symbol.
        """
        if symbol not in self._history:
            self._history[symbol] = self._empty_history_df()
        current_len = len(self._history[symbol])
        new_bars = bars[current_len:] if len(bars) > current_len else [bars[-1]]
        for bar in new_bars:
            row = {
                "date": bar.get("date"),
                "open": float(bar.get("open", 0.0)),
                "high": float(bar.get("high", 0.0)),
                "low": float(bar.get("low", 0.0)),
                "close": float(bar.get("close", 0.0)),
                "volume": float(bar.get("volume", 0.0)),
            }
            self._history[symbol] = pd.concat(
                [self._history[symbol], pd.DataFrame([row])],
                ignore_index=True,
            )
        # Re-install the get_history_df patch with the latest
        # closure (reads this symbol's history at call time).
        self._patch_get_history_df(symbol)

    def _patch_get_history_df(self, symbol: str) -> None:
        """Install per-symbol get_history_df on the strategy.

        Replaces the LAST patched version (one active patch at a
        time, since the bridge drives one symbol per on_bar).
        """
        bridge_ref = self

        def _get_history_df(
            *, count: int, symbol: str | None = None, **kwargs: Any
        ) -> pd.DataFrame:
            # Resolution order for the symbol:
            # 1. Explicit ``symbol`` kwarg (AKQuant multi-symbol API).
            # 2. ``_active_symbol`` set by the bridge before on_bar.
            # 3. Single-symbol fallback: if exactly one symbol in
            #    ``_history``, use it. This keeps legacy single-
            #    symbol tests working without explicit symbol arg.
            # 4. Otherwise empty result (multi-symbol strategy must
            #    pass symbol explicitly).
            if symbol:
                s = symbol
            elif bridge_ref._active_symbol:
                s = bridge_ref._active_symbol
            elif len(bridge_ref._history) == 1:
                s = next(iter(bridge_ref._history))
            else:
                return bridge_ref._empty_history_df()
            history = bridge_ref._history.get(s)
            if history is None or history.empty:
                return bridge_ref._empty_history_df()
            return history.tail(count).reset_index(drop=True)

        self._strategy.get_history_df = _get_history_df

    def _make_bar(self, symbol: str, latest: dict[str, Any]) -> Any:
        """Construct an AKQuant Bar-shaped object from a runner bar dict.

        AKQuant's Bar is a dataclass-like object; we use a simple
        namespace since the strategy only reads attribute access.
        The ``symbol`` attribute is set explicitly so AKQuant's
        ``bar.symbol`` returns the right value.
        """
        bar_type = type(
            "_BridgeBar",
            (),
            {
                "symbol": symbol,
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

    @property
    def symbols(self) -> list[str]:
        """Symbols seen so far (in order of first appearance)."""
        return list(self._history.keys())
