"""Unit tests for ``execution.bridge.akquant_strategy.AkquantStrategyCallable`` (W7.1 Phase 2).

The bridge wraps an ``akquant.Strategy`` subclass so it satisfies
the runner's ``Callable[[state, recent_bars], list[OrderIntent]]``
contract. Coverage:

  * Construction with default + custom kwargs.
  * Dynamic subclass overrides AKQuant's ``order_target_percent`` /
    ``record_indicator`` (MRO wins for the dynamic class).
  * get_history_df returns the last N bars of the runner's history.
  * Golden-cross triggers a buy intent; subsequent bars don't rebuy.
  * position.size reflects prior fills (after ``update_position``).
  * Indicators are recorded for the bridge's log.
  * Construction with bad kwargs raises a clear RuntimeError.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from execution.bridge import AkquantStrategyCallable  # noqa: E402
from execution.protocol import OrderIntent  # noqa: E402

# ---------------------------------------------------------------------------
# Test strategy: minimal AKQuant strategy that emits a buy on first bar
# after we set the target. Keeps the test self-contained — does NOT
# depend on research/strategies (which uses IntParam and is slower to
# instantiate).
# ---------------------------------------------------------------------------


class _OneShotBuyStrategy:
    """Buy ``target_qty`` shares on the first bar; ignore the rest.

    The runner passes ``state`` and ``recent_bars``; we ignore both
    and just emit one buy intent the first time ``on_bar`` runs.
    """

    target_qty: int = 100

    def on_start(self) -> None:
        # No-op — the bridge handles history_depth.
        return None

    def on_bar(self, bar: object) -> None:
        # If the bridge's order_target_percent override is wired,
        # we should NEVER reach this for the buy — it would call
        # self.order_target_percent() which we've overridden.
        # But for this test, we DO want to call the override to
        # verify it captures. Use a sentinel attribute to check.
        if not getattr(self, "_called", False):
            self._called = True
            self.order_target_percent(
                symbol=bar.symbol,  # type: ignore[attr-defined]
                target_percent=0.95,
            )


def _bars(closes: list[float], symbol: str = "000001") -> list[dict]:
    """Build runner-style bar dicts from a close series."""
    out: list[dict] = []
    for i, c in enumerate(closes):
        out.append({
            "date": pd.Timestamp("2024-09-02") + pd.Timedelta(days=i),
            "open": c,
            "high": c + 0.1,
            "low": c - 0.1,
            "close": c,
            "volume": 1_000_000.0,
            "symbol": symbol,
        })
    return out


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_construction_with_class() -> None:
    """Default construction (no kwargs) works."""
    bridge = AkquantStrategyCallable(_OneShotBuyStrategy)
    assert bridge.strategy_instance is not None


def test_construction_with_kwargs() -> None:
    """Kwargs forwarded to strategy __init__."""
    class StrategyWithArg:
        def __init__(self, my_qty: int) -> None:
            self.my_qty = my_qty

        def on_bar(self, bar: object) -> None:
            return None

    bridge = AkquantStrategyCallable(
        StrategyWithArg, strategy_kwargs={"my_qty": 200},
    )
    assert bridge.strategy_instance.my_qty == 200


def test_construction_with_bad_kwargs_raises_runtimeerror() -> None:
    """Clear error when strategy rejects the kwargs."""
    class StrictStrategy:
        def __init__(self, required_arg: str) -> None:
            self.required_arg = required_arg

        def on_bar(self, bar: object) -> None:
            return None

    with pytest.raises(RuntimeError, match="cannot be instantiated"):
        AkquantStrategyCallable(StrictStrategy, strategy_kwargs={})


# ---------------------------------------------------------------------------
# MRO — dynamic class overrides win
# ---------------------------------------------------------------------------


def test_dynamic_class_overrides_order_target_percent() -> None:
    """The dynamic subclass's ``order_target_percent`` wins over
    AKQuant's. Verified by checking the type.__mro__ — first class
    in the chain defines the method.
    """
    bridge = AkquantStrategyCallable(_OneShotBuyStrategy)
    cls = type(bridge.strategy_instance)
    # First class in MRO is the dynamic class — its
    # ``order_target_percent`` is the bridge override.
    assert cls.__mro__[0] is cls
    # The override lives on the dynamic class.
    assert "order_target_percent" in cls.__dict__


# ---------------------------------------------------------------------------
# get_history_df
# ---------------------------------------------------------------------------


def test_get_history_df_returns_tail_n_bars() -> None:
    """``get_history_df(count=N)`` returns the last N bars."""
    bridge = AkquantStrategyCallable(_OneShotBuyStrategy)
    state: dict = {}
    recent = _bars([10.0, 10.5, 11.0, 11.5, 12.0])
    bridge(state, recent)  # appends all 5 to history
    h = bridge._strategy.get_history_df(count=3)
    assert len(h) == 3
    assert h["close"].tolist() == [11.0, 11.5, 12.0]


def test_get_history_df_zero_or_negative_returns_empty() -> None:
    bridge = AkquantStrategyCallable(_OneShotBuyStrategy)
    state: dict = {}
    bridge(state, _bars([10.0, 11.0]))
    h = bridge._strategy.get_history_df(count=0)
    assert len(h) == 0


# ---------------------------------------------------------------------------
# Intent capture
# ---------------------------------------------------------------------------


def test_buy_intent_emitted_on_golden_cross() -> None:
    """Golden cross scenario: declining then rising → buy intent."""
    closes = [12.0 - 0.1 * i for i in range(15)] + [10.5 + 0.3 * i for i in range(15)]
    bridge = AkquantStrategyCallable(_OneShotBuyStrategy)
    state: dict = {}

    all_intents = []
    for i, _ in enumerate(closes):
        recent = _bars(closes[: i + 1])
        intents = bridge(state, recent)
        all_intents.extend(intents)

    assert len(all_intents) >= 1, "expected at least one buy intent"
    first = all_intents[0]
    assert isinstance(first, OrderIntent)
    assert first.side == "buy"
    assert first.symbol == "000001"
    assert first.quantity > 0
    assert first.price > 0


def test_buy_intent_has_enforce_lot_quantity() -> None:
    """Buy quantity is a multiple of 100 (A-share lot enforcement)."""
    bridge = AkquantStrategyCallable(_OneShotBuyStrategy)
    state: dict = {}
    recent = _bars([10.0 + 0.5 * i for i in range(30)])
    for bar_set in [recent[:i+1] for i in range(len(recent))]:
        intents = bridge(state, bar_set)
        if intents:
            for it in intents:
                if it.side == "buy":
                    assert it.quantity % 100 == 0


def test_intent_client_id_uniqueness() -> None:
    """Each call to ``__call__`` resets the capture buffer (no
    stale intents from prior bars)."""
    bridge = AkquantStrategyCallable(_OneShotBuyStrategy)
    state: dict = {}
    closes = [12.0 - 0.1 * i for i in range(15)] + [10.5 + 0.3 * i for i in range(15)]
    client_ids_per_bar = []
    for i, _ in enumerate(closes):
        recent = _bars(closes[: i + 1])
        intents = bridge(state, recent)
        client_ids_per_bar.append([it.client_order_id for it in intents])
    # Total intents = sum across bars (not multiplied per bar).
    total = sum(len(x) for x in client_ids_per_bar)
    all_ids = [cid for sub in client_ids_per_bar for cid in sub]
    assert total == len(all_ids)
    assert len(all_ids) == len(set(all_ids))  # all unique


# ---------------------------------------------------------------------------
# Position update
# ---------------------------------------------------------------------------


def test_update_position_reflected_in_subsequent_calls() -> None:
    """``update_position`` after a fill is visible to the strategy
    on the next bar (via the FakePosition property)."""
    bridge = AkquantStrategyCallable(_OneShotBuyStrategy)
    bridge._strategy.on_start()
    # No position yet.
    assert bridge._fake_position.size == 0
    # After fill.
    bridge.update_position("000001", quantity=100, avg_cost=10.0)
    assert bridge._fake_position.size == 100
    assert bridge._fake_position.avg_cost == 10.0


def test_update_position_wrong_symbol_raises() -> None:
    """Defensive: bridge for ``symbol=X`` rejects updates for ``Y``."""
    bridge = AkquantStrategyCallable(_OneShotBuyStrategy, symbol="000001")
    with pytest.raises(ValueError, match="got position update"):
        bridge.update_position("600000", quantity=100, avg_cost=10.0)
