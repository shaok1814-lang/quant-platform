"""Unit tests for ``ops.cross_source_job`` (W6.3).

All tests are offline — they inject stub fetchers for both akshare
and baostock so no real HTTP / network call is made. Each test owns
its own tiny ``universe.yaml`` + stub fetcher so the suite runs
deterministically without state leak.

Coverage:

* Happy path: stub fetchers return identical prices → all
  ``passed``, no 钉聊 fired.
* Per-symbol failure: one symbol's diff exceeds the bps threshold →
  ``status="failed"``, ``n_failed=1``, alert channel called.
* Per-symbol fetcher failures: only-akshare / only-baostock /
  both-fail map to the corresponding ``skipped_*`` status without
  aborting the loop.
* Threshold parameter propagation: a tighter threshold reclassifies
  small diffs as ``failed``.
* Aggregation: ``n_passed`` / ``n_failed`` / ``n_skipped`` properties
  count correctly across mixed-status runs.
* ``notify_on_fail=False`` short-circuits the 钉聊 channel (the
  default for tests; this test pins that behaviour).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from datetime import date as date_cls
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_layer.ingestion.akshare_fetcher import FetcherError as AkshareFetcherError  # noqa: E402
from data_layer.ingestion.baostock_fetcher import FetcherError as BaostockFetcherError  # noqa: E402
from ops.cross_source_job import (  # noqa: E402
    DEFAULT_THRESHOLD_BPS,
    CrossSourceReport,
    PerSymbolDiffReport,
    _to_baostock_date,
    run_cross_source_check,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _write_universe(tmp_path: Path, symbols: list[tuple[str, str, str]]) -> Path:
    """Write a tiny ``universe.yaml`` (mirrors ``tests/test_ingest_job.py``)."""
    lines = ["universe:"]
    for sym, name, sector in symbols:
        lines.append(f"  - {{symbol: '{sym}', name: '{name}', sector: '{sector}'}}")
    p = tmp_path / "universe.yaml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _make_stub_fetcher(
    mapper: dict[str, pd.DataFrame | Exception],
    *,
    fetcher_name: str,
    fetcher_error_cls: type[Exception],
) -> object:
    """Build a stub fetcher that mirrors ``tests/test_ingest_job._make_fetcher``.

    Args:
        mapper: ``symbol -> df-or-exception``. Symbols missing from
            the mapper raise ``fetcher_error_cls`` (simulates a
            network / delisted / holiday outage for that symbol).
        fetcher_name: Value to put on ``df.attrs['fetcher']``
            (so ``validate`` echoes the right label).
        fetcher_error_cls: Exception class to raise for missing
            symbols (mirror the real fetcher's
            ``FetcherError`` semantics).
    """

    def _fetch(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        if symbol not in mapper:
            raise fetcher_error_cls(f"stub {fetcher_name}: no data for {symbol}")
        item = mapper[symbol]
        if isinstance(item, BaseException):
            raise item
        df = item.copy()
        df.attrs["symbol"] = symbol
        df.attrs["fetcher"] = fetcher_name
        df.attrs["adjust"] = "qfq"
        df.attrs["fetched_at"] = datetime.now(UTC).isoformat()
        return df

    return _fetch


def _akshare_stub(mapper: dict[str, pd.DataFrame | Exception]) -> object:
    """Convenience: build an akshare stub with the right fetcher name + error class."""
    return _make_stub_fetcher(mapper, fetcher_name="akshare", fetcher_error_cls=AkshareFetcherError)


def _baostock_stub(mapper: dict[str, pd.DataFrame | Exception]) -> object:
    """Convenience: build a baostock stub with the right fetcher name + error class."""
    return _make_stub_fetcher(
        mapper,
        fetcher_name="baostock",
        fetcher_error_cls=BaostockFetcherError,
    )


def _good_df(symbol: str = "000001") -> pd.DataFrame:
    """One-bar synthetic df for the test target date.

    Matches the column shape of the real fetchers (CORE_COLUMNS from
    ``data_layer/ingestion/akshare_fetcher.py``).
    """
    return pd.DataFrame(
        {
            "date": [pd.Timestamp(date_cls(2026, 8, 31))],
            "open": [10.00],
            "high": [10.50],
            "low": [9.95],
            "close": [10.20],
            "volume": [1_000_000.0],
            "amount": [10_000_000.0],
        }
    )


def _multi_bar_df(symbol: str, closes: list[float], start: str = "2026-08-25") -> pd.DataFrame:
    """Multi-bar df with explicit close prices (overrides ``make_bars``).

    We construct the df directly (not via ``make_bars``) so the test
    can drive the close values precisely for bps-threshold edge
    cases without the ``make_bars`` defaults leaking in.
    """
    n = len(closes)
    dates = pd.bdate_range(end=pd.Timestamp(start), periods=n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [c + 0.05 for c in closes],
            "low": [c - 0.05 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * n,
            "amount": [10_000_000.0] * n,
        }
    )


# ---------------------------------------------------------------------------
# Group 1: helper unit tests
# ---------------------------------------------------------------------------


def test_to_baostock_date_from_compact() -> None:
    """``YYYYMMDD`` → ``YYYY-MM-DD``."""
    assert _to_baostock_date("20260831") == "2026-08-31"


def test_to_baostock_date_already_dashed() -> None:
    """Already-ISO string passes through unchanged."""
    assert _to_baostock_date("2026-08-31") == "2026-08-31"


def test_to_baostock_date_invalid_raises() -> None:
    """Garbage input → ``ValueError`` (defensive)."""
    with pytest.raises(ValueError, match="YYYYMMDD"):
        _to_baostock_date("nope")


# ---------------------------------------------------------------------------
# Group 2: happy path
# ---------------------------------------------------------------------------


def test_happy_path_all_pass(tmp_path: Path) -> None:
    """Both fetchers return identical prices → all ``passed``, no 钉聊."""
    universe = _write_universe(tmp_path, [("000001", "A", "bank")])
    same_df = _good_df()
    ak = _akshare_stub({"000001": same_df})
    bs = _baostock_stub({"000001": same_df})

    report = run_cross_source_check(
        date=date_cls(2026, 8, 31),
        universe_path=universe,
        akshare_fetcher=ak,
        baostock_fetcher=bs,
        notify_on_fail=False,
    )

    assert isinstance(report, CrossSourceReport)
    assert report.target_date == date_cls(2026, 8, 31)
    assert report.threshold_bps == DEFAULT_THRESHOLD_BPS
    assert len(report.per_symbol) == 1
    assert report.per_symbol[0].status == "passed"
    assert report.per_symbol[0].symbol == "000001"
    assert report.per_symbol[0].fetcher_a == "akshare"
    assert report.per_symbol[0].fetcher_b == "baostock"
    assert report.n_passed == 1
    assert report.n_failed == 0
    assert report.n_skipped == 0


# ---------------------------------------------------------------------------
# Group 3: per-symbol diff failure
# ---------------------------------------------------------------------------


def test_one_symbol_fails_threshold(tmp_path: Path) -> None:
    """Single symbol diff > threshold → ``failed``, alert fires."""
    universe = _write_universe(
        tmp_path,
        [("000001", "A", "bank"), ("000002", "B", "tech")],
    )
    # 000001: same → passed. 000002: 1% off → ~100bps → failed.
    ak = _akshare_stub(
        {
            "000001": _multi_bar_df("000001", [100.0]),
            "000002": _multi_bar_df("000002", [100.0]),
        }
    )
    bs = _baostock_stub(
        {
            "000001": _multi_bar_df("000001", [100.0]),
            "000002": _multi_bar_df("000002", [101.0]),  # ~100bps off
        }
    )

    # Spy on notify.ding to confirm the SOFT alert fires.
    import ops.notify as notify_mod

    sent: list[tuple[str, str]] = []
    real_ding = notify_mod.ding

    def _spy_ding(title: str, body: str, **kwargs):  # type: ignore[no-untyped-def]
        sent.append((title, body))
        return real_ding(title, body, **kwargs)

    notify_mod.ding = _spy_ding  # type: ignore[assignment]
    try:
        report = run_cross_source_check(
            date=date_cls(2026, 8, 31),
            universe_path=universe,
            akshare_fetcher=ak,
            baostock_fetcher=bs,
            notify_on_fail=True,
        )
    finally:
        notify_mod.ding = real_ding  # type: ignore[assignment]

    assert report.n_passed == 1
    assert report.n_failed == 1
    assert report.n_skipped == 0
    failed = next(r for r in report.per_symbol if r.symbol == "000002")
    assert failed.status == "failed"
    assert failed.max_pct_diff_bps > 50.0
    # 钉聊 was called exactly once with the right title prefix.
    assert len(sent) == 1
    title, body = sent[0]
    assert "Cross-source diff report" in title
    assert "000002" in body


# ---------------------------------------------------------------------------
# Group 4: per-symbol fetcher failures
# ---------------------------------------------------------------------------


def test_per_symbol_isolation(tmp_path: Path) -> None:
    """Mixed: 000001 passed, 000002 failed, 000003 skipped_akshare.

    No single failure blocks the rest of the universe.
    """
    universe = _write_universe(
        tmp_path,
        [
            ("000001", "A", "bank"),
            ("000002", "B", "tech"),
            ("000003", "C", "finance"),
        ],
    )
    ak = _akshare_stub(
        {
            "000001": _multi_bar_df("000001", [100.0]),
            "000002": _multi_bar_df("000002", [100.0]),
            # 000003 missing → akshare raises
        }
    )
    bs = _baostock_stub(
        {
            "000001": _multi_bar_df("000001", [100.0]),
            "000002": _multi_bar_df("000002", [101.0]),
            "000003": _multi_bar_df("000003", [50.0]),
        }
    )

    report = run_cross_source_check(
        date=date_cls(2026, 8, 31),
        universe_path=universe,
        akshare_fetcher=ak,
        baostock_fetcher=bs,
        notify_on_fail=False,
    )

    statuses = {r.symbol: r.status for r in report.per_symbol}
    assert statuses == {
        "000001": "passed",
        "000002": "failed",
        "000003": "skipped_akshare",
    }
    assert report.n_passed == 1
    assert report.n_failed == 1
    assert report.n_skipped == 1


def test_both_fetchers_fail(tmp_path: Path) -> None:
    """Both fetchers raise → ``skipped_both``."""
    universe = _write_universe(tmp_path, [("000001", "A", "bank")])
    ak = _akshare_stub({})  # missing → AkshareFetcherError
    bs = _baostock_stub({})  # missing → BaostockFetcherError

    report = run_cross_source_check(
        date=date_cls(2026, 8, 31),
        universe_path=universe,
        akshare_fetcher=ak,
        baostock_fetcher=bs,
        notify_on_fail=False,
    )

    assert len(report.per_symbol) == 1
    r = report.per_symbol[0]
    assert r.status == "skipped_both"
    assert r.error is not None
    assert report.n_passed == 0
    assert report.n_failed == 0
    assert report.n_skipped == 1


def test_only_akshare_fails(tmp_path: Path) -> None:
    """akshare raises, baostock OK → ``skipped_akshare``."""
    universe = _write_universe(tmp_path, [("000001", "A", "bank")])
    ak = _akshare_stub({})
    bs = _baostock_stub({"000001": _good_df()})

    report = run_cross_source_check(
        date=date_cls(2026, 8, 31),
        universe_path=universe,
        akshare_fetcher=ak,
        baostock_fetcher=bs,
        notify_on_fail=False,
    )
    r = report.per_symbol[0]
    assert r.status == "skipped_akshare"
    assert "akshare" in (r.error or "").lower()
    assert report.n_skipped == 1


def test_only_baostock_fails(tmp_path: Path) -> None:
    """baostock raises, akshare OK → ``skipped_baostock``."""
    universe = _write_universe(tmp_path, [("000001", "A", "bank")])
    ak = _akshare_stub({"000001": _good_df()})
    bs = _baostock_stub({})

    report = run_cross_source_check(
        date=date_cls(2026, 8, 31),
        universe_path=universe,
        akshare_fetcher=ak,
        baostock_fetcher=bs,
        notify_on_fail=False,
    )
    r = report.per_symbol[0]
    assert r.status == "skipped_baostock"
    assert "baostock" in (r.error or "").lower()
    assert report.n_skipped == 1


def test_empty_df_non_trading_day(tmp_path: Path) -> None:
    """Both fetchers return empty df → ``skipped_both`` (not a fetcher error)."""
    universe = _write_universe(tmp_path, [("000001", "A", "bank")])
    empty = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
    ak = _akshare_stub({"000001": empty})
    bs = _baostock_stub({"000001": empty})

    report = run_cross_source_check(
        date=date_cls(2026, 8, 31),
        universe_path=universe,
        akshare_fetcher=ak,
        baostock_fetcher=bs,
        notify_on_fail=False,
    )
    r = report.per_symbol[0]
    assert r.status == "skipped_both"
    assert "empty" in (r.error or "")
    assert report.n_skipped == 1


# ---------------------------------------------------------------------------
# Group 5: threshold parameter propagation
# ---------------------------------------------------------------------------


def test_threshold_parameter_propagates(tmp_path: Path) -> None:
    """Tighter threshold reclassifies small diffs as ``failed``."""
    universe = _write_universe(tmp_path, [("000001", "A", "bank")])
    # 5bps gap → fails 5bps threshold, passes 50bps threshold.
    ak = _akshare_stub({"000001": _multi_bar_df("000001", [100.0])})
    bs = _baostock_stub({"000001": _multi_bar_df("000001", [100.05])})

    tight = run_cross_source_check(
        date=date_cls(2026, 8, 31),
        universe_path=universe,
        akshare_fetcher=ak,
        baostock_fetcher=bs,
        threshold_bps=1.0,  # 5bps > 1bps → fail
        notify_on_fail=False,
    )
    assert tight.per_symbol[0].status == "failed"
    assert tight.threshold_bps == 1.0

    loose = run_cross_source_check(
        date=date_cls(2026, 8, 31),
        universe_path=universe,
        akshare_fetcher=ak,
        baostock_fetcher=bs,
        threshold_bps=100.0,  # 5bps < 100bps → pass
        notify_on_fail=False,
    )
    assert loose.per_symbol[0].status == "passed"
    assert loose.threshold_bps == 100.0


# ---------------------------------------------------------------------------
# Group 6: notify behaviour
# ---------------------------------------------------------------------------


def test_notify_disabled_no_ding(tmp_path: Path) -> None:
    """``notify_on_fail=False`` must NOT touch the 钉聊 channel."""
    universe = _write_universe(tmp_path, [("000001", "A", "bank")])
    ak = _akshare_stub({"000001": _multi_bar_df("000001", [100.0])})
    bs = _baostock_stub({"000001": _multi_bar_df("000001", [200.0])})  # 100% off → huge bps

    import ops.notify as notify_mod

    sent: list[tuple[str, str]] = []
    real_ding = notify_mod.ding

    def _spy_ding(title: str, body: str, **kwargs):  # type: ignore[no-untyped-def]
        sent.append((title, body))
        return real_ding(title, body, **kwargs)

    notify_mod.ding = _spy_ding  # type: ignore[assignment]
    try:
        report = run_cross_source_check(
            date=date_cls(2026, 8, 31),
            universe_path=universe,
            akshare_fetcher=ak,
            baostock_fetcher=bs,
            notify_on_fail=False,  # ← key flag
        )
    finally:
        notify_mod.ding = real_ding  # type: ignore[assignment]

    assert report.n_failed == 1
    # Despite a failure, the channel was not invoked.
    assert sent == []


def test_notify_not_called_when_all_pass(tmp_path: Path) -> None:
    """All symbols pass → ``n_failed == 0`` → no 钉聊 even if enabled."""
    universe = _write_universe(tmp_path, [("000001", "A", "bank")])
    same_df = _good_df()
    ak = _akshare_stub({"000001": same_df})
    bs = _baostock_stub({"000001": same_df})

    import ops.notify as notify_mod

    sent: list[tuple[str, str]] = []
    real_ding = notify_mod.ding

    def _spy_ding(title: str, body: str, **kwargs):  # type: ignore[no-untyped-def]
        sent.append((title, body))
        return real_ding(title, body, **kwargs)

    notify_mod.ding = _spy_ding  # type: ignore[assignment]
    try:
        run_cross_source_check(
            date=date_cls(2026, 8, 31),
            universe_path=universe,
            akshare_fetcher=ak,
            baostock_fetcher=bs,
            notify_on_fail=True,
        )
    finally:
        notify_mod.ding = real_ding  # type: ignore[assignment]

    assert sent == []


# ---------------------------------------------------------------------------
# Group 7: aggregation properties
# ---------------------------------------------------------------------------


def test_aggregate_properties_match_per_symbol(tmp_path: Path) -> None:
    """``n_passed`` / ``n_failed`` / ``n_skipped`` count across mixed run."""
    universe = _write_universe(
        tmp_path,
        [
            ("000001", "A", "bank"),  # passed
            ("000002", "B", "tech"),  # failed
            ("000003", "C", "finance"),  # skipped_akshare
            ("000004", "D", "energy"),  # skipped_baostock
            ("000005", "E", "etf"),  # skipped_both
            ("000006", "F", "consumer"),  # passed
        ],
    )
    ok = _good_df()

    # 000002 baostock: same DATE as the others (so dates overlap and
    # validate produces a real bps diff), but close=11.00 vs akshare
    # 10.20 → ~770bps → clearly failed at 50bps threshold.
    bs_000002_failed = pd.DataFrame(
        {
            "date": [pd.Timestamp(date_cls(2026, 8, 31))],
            "open": [11.00],
            "high": [11.10],
            "low": [10.90],
            "close": [11.00],
            "volume": [1_000_000.0],
            "amount": [10_000_000.0],
        }
    )

    ak = _akshare_stub(
        {
            "000001": ok,
            "000002": ok,
            # 000003 missing → skipped_akshare
            "000004": ok,
            # 000005 missing → skipped_both (with bs also missing)
            "000006": ok,
        }
    )
    bs = _baostock_stub(
        {
            "000001": ok,
            "000002": bs_000002_failed,
            "000003": ok,  # baostock has it but akshare doesn't
            # 000004 missing → skipped_baostock
            # 000005 missing → skipped_both
            "000006": ok,
        }
    )

    report = run_cross_source_check(
        date=date_cls(2026, 8, 31),
        universe_path=universe,
        akshare_fetcher=ak,
        baostock_fetcher=bs,
        notify_on_fail=False,
    )

    statuses = {r.symbol: r.status for r in report.per_symbol}
    assert statuses["000001"] == "passed"
    assert statuses["000002"] == "failed"
    assert statuses["000003"] == "skipped_akshare"
    assert statuses["000004"] == "skipped_baostock"
    assert statuses["000005"] == "skipped_both"
    assert statuses["000006"] == "passed"
    assert report.n_passed == 2
    assert report.n_failed == 1
    # n_skipped counts all three skip variants.
    assert report.n_skipped == 3
    # Sanity: all three counts plus the per-symbol list are
    # internally consistent (every per-symbol report is bucketed
    # into exactly one of pass / fail / skip).
    bucketed = report.n_passed + report.n_failed + report.n_skipped
    assert bucketed == len(report.per_symbol)


def test_per_symbol_dataclass_is_frozen(tmp_path: Path) -> None:
    """``PerSymbolDiffReport`` is ``frozen=True`` (mirrors
    ``IngestReport.PerSymbolReport``). Touching a field raises."""
    psr = PerSymbolDiffReport(
        symbol="000001",
        sector="bank",
        status="passed",
    )
    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError on stdlib dataclass
        psr.status = "failed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Group 8: opt-in network smoke (skipped by default)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="network test — requires akshare and baostock access. Run "
    "manually with `uv run pytest tests/test_cross_source_job.py -k smoke` "
    "after opting in. Will likely fail under the W2.1 Windows-netsh "
    "proxy issue."
)
def test_smoke_000001_real_network() -> None:
    """Real akshare + baostock dual-fetch on 000001.

    Useful as a release sign-off; not part of CI. Expected behaviour:
    ``passed`` if both fetchers are reachable and the qfq-adjusted
    close prices agree within 50bps; ``skipped_*`` if either
    fetcher is blocked.
    """
    report = run_cross_source_check(
        date=date_cls(2026, 8, 31),
        notify_on_fail=False,
    )
    sym_report = next(r for r in report.per_symbol if r.symbol == "000001")
    # Either passed (within 50bps) or skipped (network blocked).
    assert sym_report.status in ("passed", "skipped_akshare", "skipped_baostock", "skipped_both")
    # If we did get a diff, it must be within the tolerance that
    # ``passed`` would imply.
    if sym_report.status == "passed":
        assert sym_report.n_overlap >= 1
        assert sym_report.max_pct_diff_bps <= DEFAULT_THRESHOLD_BPS
