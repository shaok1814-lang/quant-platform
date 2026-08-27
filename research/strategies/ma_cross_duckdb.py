"""MA-Cross driven from DuckDB (W2.1 sanity check).

Pulls bars through the new data layer (fetcher -> parquet -> DuckStore
-> query) and runs the same MA-cross strategy used in
``ma_cross.py``. The output metrics are compared against
``[[ma-cross-baseline-000001-20240826]]`` to detect any drift introduced
by the parquet / DuckDB round-trip.

Note: the akshare-direct baseline (``ma_cross.py``) and this DuckDB
run do *not* produce byte-identical metrics because the two code paths
fetch slightly different price windows (akshare occasionally
back-fills / re-stitches a few bars between calls). The expectation is
that drift stays within ~0.5% on every metric and the closed-trade
count matches.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Bootstrap sys.path so ``uv run python research/strategies/X.py``
# resolves top-level packages (``data_layer`` / ``research`` / ...)
# the same way pytest does via ``pythonpath = ["."]``. Without this,
# ``python file.py`` only adds the file's directory to sys.path and
# ``import data_layer`` fails.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from akquant import ChinaStockConfig, run_backtest  # noqa: E402
from akquant.config import (  # noqa: E402
    BacktestConfig,
    InstrumentConfig,
    RiskConfig,
    StrategyConfig,
)
from data_layer.ingestion.akshare_fetcher import fetch_daily_bars  # noqa: E402
from data_layer.storage.duck import DuckStore  # noqa: E402
from loguru import logger  # noqa: E402

from research.strategies.ma_cross import (  # noqa: E402
    COMMISSION_RATE,
    HISTORY_DEPTH,
    INITIAL_CASH,
    LOT_SIZE,
    SLOW_WINDOW,
    STAMP_TAX_RATE,
    TARGET_PERCENT,
    MACrossStrategy,
)

# Hard-coded to mirror ma_cross.SYMBOL / START_DATE / END_DATE so this
# file can be diffed against the baseline memory entry without reading
# ma_cross.py at runtime.
SYMBOL: str = "000001"
START_DATE_RAW: str = "20240901"  # YYYYMMDD for akshare
END_DATE_RAW: str = "20260825"
START_DATE_ISO: str = "2024-09-01"  # YYYY-MM-DD for DuckDB DATE cast
END_DATE_ISO: str = "2026-08-25"
DUCKDB_PATH: Path = Path("data/duckdb/daily.duckdb")


def _row(metrics: object, name: str) -> float:
    try:
        return float(metrics.loc[name, "value"])  # type: ignore[attr-defined]
    except (KeyError, TypeError, ValueError):
        return float("nan")


def run_duckdb_demo() -> object:
    """Fetch → DuckDB upsert → query → run_backtest → metrics."""
    df = fetch_daily_bars(SYMBOL, START_DATE_RAW, END_DATE_RAW)
    logger.info("fetched {n} bars from akshare", n=len(df))

    DUCKDB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DuckStore(DUCKDB_PATH) as store:
        store.upsert_daily_bars(df)
        out = store.query_daily_bars(
            SYMBOL, start_date=START_DATE_ISO, end_date=END_DATE_ISO
        )
    logger.info("read {n} bars from DuckDB", n=len(out))
    if len(out) != len(df):
        logger.warning(
            "row count mismatch: akshare={a} duckdb={d}", a=len(df), d=len(out)
        )

    result = run_backtest(
        data=out,
        strategy=MACrossStrategy,
        symbols=[SYMBOL],
        initial_cash=INITIAL_CASH,
        commission_rate=COMMISSION_RATE,
        stamp_tax_rate=STAMP_TAX_RATE,
        lot_size=LOT_SIZE,
        t_plus_one=True,
        history_depth=HISTORY_DEPTH,
        warmup_period=SLOW_WINDOW,
        config=BacktestConfig(
            strategy_config=StrategyConfig(
                initial_cash=INITIAL_CASH,
                risk=RiskConfig(max_position_pct=TARGET_PERCENT),
            ),
            instruments_config=[
                InstrumentConfig(
                    symbol=SYMBOL, asset_type="STOCK",
                    tick_size=0.01, lot_size=LOT_SIZE,
                ),
            ],
            china_stock=ChinaStockConfig(enforce_tick_size=True),
            show_progress=False,
        ),
    )

    metrics = result.metrics_df
    logger.success(
        "MA-cross (DuckDB): bars={n} trades={nt} total_ret={ret:.2f}% "
        "sharpe={sh:.3f} sortino={so:.3f} mdd={dd:.2%} win_rate={wr:.2f}% "
        "profit_factor={pf:.2f} exposure={ex:.2f}% max_lev={ml:.2f}",
        n=len(out), nt=len(result.trades_df),
        ret=_row(metrics, "total_return_pct"),
        sh=_row(metrics, "sharpe_ratio"),
        so=_row(metrics, "sortino_ratio"),
        dd=_row(metrics, "max_drawdown"),
        wr=_row(metrics, "win_rate"),
        pf=_row(metrics, "profit_factor"),
        ex=_row(metrics, "exposure_time_pct"),
        ml=_row(metrics, "max_leverage"),
    )
    return result


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO", colorize=True)
    run_duckdb_demo()
