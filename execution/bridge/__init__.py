"""Strategy bridge — let AKQuant ``Strategy`` subclasses feed the runner.

The runner's strategy contract is
``Callable[[state, recent_bars], list[OrderIntent]]``. AKQuant
strategies (``research/strategies/*.py``) follow a different
contract: they subclass ``akquant.Strategy`` and call
``self.order_target_percent(symbol=, target_percent=)`` from
``on_bar``. To bridge the gap, this package wraps an AKQuant
strategy into a callable the runner can drive.

Usage::

    from execution.bridge import AkquantStrategyCallable
    from research.strategies.ma_cross import MACrossStrategy

    bridge = AkquantStrategyCallable(MACrossStrategy)
    report = run_paper_session(strategy=bridge, data=bars, ...)

The bridge owns:
  * a fake-position mirror (so ``self.position.size`` returns the
    right value when the strategy asks),
  * a fake-history DataFrame (so ``self.get_history_df(count=N)``
    returns the last N bars),
  * an intent-capture hook (so ``order_target_percent`` records
    OrderIntent instead of submitting through AKQuant's normal
    execution backend).
"""

from execution.bridge.akquant_strategy import (
    AkquantStrategyCallable,
    FakePosition,
)

__all__ = ["AkquantStrategyCallable", "FakePosition"]
