"""Refresh ST + delisted A-share snapshots from akshare.

Writes two CSVs that ``backtest.a_share.filter_st`` /
``backtest.a_share.build_universe`` consume offline:

  - ``data/st_a_share_list.csv`` — column ``代码``
  - ``data/delisted_a_share_list.csv`` — column ``证券代码``
    (SZSE convention; the offline reader also accepts ``代码`` /
    ``公司代码``)

The ST snapshot uses ``ak.stock_info_a_code_name()`` filtered by
name prefix ``ST`` / ``*ST``. This is more reliable than
``ak.stock_zh_a_st_em()`` which has intermittent proxy failures
(see W2.1 status). ``stock_info_a_code_name`` returns the full
~5500-symbol code→name list (one round trip), and the name prefix
filter is deterministic + offline-encodable.

The delisted snapshot uses two akshare endpoints (SSE + SZSE).
STAQ/NET is best-effort: failure logs WARNING and continues.

Idempotent: re-running overwrites the CSVs in place.

**Usage**::

    uv run python scripts/snapshot_st_delisted.py            # refresh both CSVs
    uv run python scripts/snapshot_st_delisted.py --dry-run  # print counts, no write
    uv run python scripts/snapshot_st_delisted.py --only st # refresh ST only

Cron-able: see ``ops/scheduler.py`` if you want to wire this as a
weekly cron alongside ``cross_source_job``.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# Allow `python scripts/snapshot_st_delisted.py` without ``PYTHONPATH=.``.
_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import akshare as ak  # noqa: E402
from loguru import logger  # noqa: E402

from backtest.a_share import fetch_delisted_symbols, fetch_st_symbols  # noqa: E402

DEFAULT_ST_CSV: Path = _PROJECT_ROOT / "data" / "st_a_share_list.csv"
DEFAULT_DELISTED_CSV: Path = _PROJECT_ROOT / "data" / "delisted_a_share_list.csv"


def _refresh_st(out_path: Path) -> int:
    """Refresh the ST snapshot. Returns the symbol count written."""
    df = ak.stock_info_a_code_name()
    # akshare column names vary across versions; tolerate both English + 中文.
    name_col = next((c for c in df.columns if c in ("name", "名称")), None)
    code_col = next((c for c in df.columns if c in ("code", "代码")), None)
    if name_col is None or code_col is None:
        raise RuntimeError(
            f"ak.stock_info_a_code_name() returned unexpected columns: {list(df.columns)}"
        )
    mask = df[name_col].astype(str).str.startswith(("ST", "*ST"))
    codes = sorted(
        c.zfill(6)
        for c in df.loc[mask, code_col].astype(str).tolist()
        if c.strip().isdigit() or (c.strip().zfill(6).isdigit() and len(c.strip().zfill(6)) == 6)
    )
    codes = [c for c in codes if len(c) == 6 and c.isdigit()]
    codes = sorted(set(codes))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        f.write(f"# A-share ST snapshot, refreshed {date.today().isoformat()}\n")
        f.write("# Source: akshare stock_info_a_code_name() filtered by name prefix (ST / *ST)\n")
        f.write("代码\n")
        for s in codes:
            f.write(f"{s}\n")
    logger.info("ST snapshot: wrote {n} symbols to {p}", n=len(codes), p=out_path)
    return len(codes)


def _refresh_delisted(out_path: Path) -> int:
    """Refresh the delisted snapshot. Returns the symbol count written.

    Uses ``fetch_delisted_symbols`` which already wraps the akshare
    endpoints + offline fallback; here we just write the result.
    """
    codes = sorted(fetch_delisted_symbols(allow_network=True))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as f:
        f.write(f"# A-share delisted snapshot, refreshed {date.today().isoformat()}\n")
        f.write("# Source: akshare stock_info_sh_delist + stock_info_sz_delist\n")
        f.write("证券代码\n")
        for s in codes:
            f.write(f"{s}\n")
    logger.info("Delisted snapshot: wrote {n} symbols to {p}", n=len(codes), p=out_path)
    return len(codes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--only",
        choices=("st", "delisted"),
        help="Refresh only one of the two snapshots (default: both).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts without writing files.",
    )
    parser.add_argument("--st-csv", type=Path, default=DEFAULT_ST_CSV)
    parser.add_argument("--delisted-csv", type=Path, default=DEFAULT_DELISTED_CSV)
    args = parser.parse_args(argv)

    today = date.today().isoformat()
    n_st = n_dl = 0

    if args.only != "delisted":
        try:
            df = ak.stock_info_a_code_name()
            name_col = next(c for c in df.columns if c in ("name", "名称"))
            code_col = next(c for c in df.columns if c in ("code", "代码"))
            n_st = int(df[name_col].astype(str).str.startswith(("ST", "*ST")).sum())
        except Exception as exc:  # pragma: no cover — network path
            logger.error("ST fetch failed: {exc}", exc=exc)
            n_st = -1

    if args.only != "st":
        try:
            n_dl = len(fetch_delisted_symbols(allow_network=True))
        except Exception as exc:  # pragma: no cover — network path
            logger.error("Delisted fetch failed: {exc}", exc=exc)
            n_dl = -1

    print(f"[{today}] ST={n_st} Delisted={n_dl}")

    if args.dry_run:
        return 0

    if args.only != "delisted" and n_st > 0:
        _refresh_st(args.st_csv)
    if args.only != "st" and n_dl > 0:
        _refresh_delisted(args.delisted_csv)

    return 0


if __name__ == "__main__":
    sys.exit(main())
