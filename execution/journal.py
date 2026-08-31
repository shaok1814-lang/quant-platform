"""SQLite-backed paper-trade journal (W7.1).

Persists every intent / report / fill / snapshot the runner produces
to a single SQLite file. Two purposes per CLAUDE.md 「合规与实盘纪律」:

  1. **4-week paper-trading replay** — query fills by date to inspect
     what the strategy actually did, no broker round-trip needed.

  2. **Mock-vs-live deviation check (Phase 2)** — ``compare_to()``
     compares two journals (paper session vs. live session with the
     same intent stream) and surfaces symbols / bars where the
     execution outcome differs by more than ``max_deviation_pct``.

Why SQLite (not Parquet / JSONL):
  * stdlib only (CLAUDE.md 「不要主动建议引入新的依赖」)
  * atomic writes per row (no partial-commit risk on power loss)
  * queryable from any tool that ships with Python
  * single file per session — easy to archive / ship to the broker
    for dispute resolution

Schema design notes:

  * ``order_intent.client_order_id`` is the PK. The runner
    re-records the same intent with a different risk_decision on
    resubmission (UPDATE on conflict), not INSERT OR IGNORE — so
    the journal surfaces the final disposition.
  * ``execution_report.client_order_id`` PK with FK semantics
    implied (no actual FK constraint — keeps the schema migratable
    and tests don't need to set up the parent row first).
  * ``fill.fill_id`` is its own PK (uuid4), since one client_order_id
    may produce multiple fills (partial fills, multi-leg, etc.).
  * Timestamps stored as ISO-8601 strings, not integers. ISO strings
    are greppable, comparable lexically, and avoid the epoch-vs-
    nanosecond confusion that bit AKQuant's gateway models.

Threading: SQLite connection is created per-method (sqlite3 default).
For Phase 2 (XtQuant live + callbacks on SDK thread), the runner
should serialize writes through a single lock; we leave that to
the runner, not the journal.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from typing import Final

from execution.protocol import (
    EquitySnapshot,
    ExecutionReport,
    Fill,
    OrderIntent,
)
from execution.risk import Allow, Reject, RiskDecision

__all__ = [
    "DEFAULT_JOURNAL_DIR",
    "JOURNAL_SCHEMA_VERSION",
    "CompareResult",
    "DeviationRow",
    "PaperJournal",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default journal directory: project-root / data / journal.
_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_JOURNAL_DIR: Final[Path] = _PROJECT_ROOT / "data" / "journal"

# Bump whenever the schema changes in an incompatible way. Tests
# pin this so a forgotten migration fails loud, not silent.
JOURNAL_SCHEMA_VERSION: Final[int] = 1


# Schema (one constant per table). Reused by tests that want to
# inspect the schema without opening a file.
_SCHEMA_ORDER_INTENT: Final[str] = """
CREATE TABLE IF NOT EXISTS order_intent (
    client_order_id TEXT PRIMARY KEY,
    bar_timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL,
    order_type TEXT NOT NULL,
    reason TEXT,
    risk_decision TEXT NOT NULL,
    risk_reason TEXT
)
"""
_SCHEMA_EXECUTION_REPORT: Final[str] = """
CREATE TABLE IF NOT EXISTS execution_report (
    client_order_id TEXT PRIMARY KEY,
    broker_order_id TEXT,
    status TEXT NOT NULL,
    filled_quantity INTEGER NOT NULL DEFAULT 0,
    avg_fill_price REAL,
    reject_reason TEXT,
    timestamp TEXT
)
"""
_SCHEMA_FILL: Final[str] = """
CREATE TABLE IF NOT EXISTS fill (
    fill_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    broker_order_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    stamp_tax REAL NOT NULL DEFAULT 0,
    timestamp TEXT NOT NULL
)
"""
_SCHEMA_EQUITY_SNAPSHOT: Final[str] = """
CREATE TABLE IF NOT EXISTS equity_snapshot (
    timestamp TEXT PRIMARY KEY,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    total_equity REAL NOT NULL,
    drawdown_pct REAL NOT NULL
)
"""
_SCHEMA_META: Final[str] = """
CREATE TABLE IF NOT EXISTS journal_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


# ---------------------------------------------------------------------------
# Compare result dataclass (Phase 2 hook)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviationRow:
    """One row of deviation between two journals.

    Attributes:
        client_order_id: The intent that produced divergent fills.
        symbol: 6-digit symbol.
        paper_quantity: Quantity filled in the paper journal.
        live_quantity: Quantity filled in the live journal.
        paper_price: Avg fill price on the paper side.
        live_price: Avg fill price on the live side.
        quantity_deviation_pct: ``abs(paper - live) / max(paper, 1)``.
        price_deviation_pct: ``abs(paper - live) / max(paper, 1e-6)``.
    """

    client_order_id: str
    symbol: str
    paper_quantity: int
    live_quantity: int
    paper_price: float | None
    live_price: float | None
    quantity_deviation_pct: float
    price_deviation_pct: float


@dataclass(frozen=True)
class CompareResult:
    """Outcome of :meth:`PaperJournal.compare_to`.

    Attributes:
        max_deviation_pct: Threshold passed to ``compare_to``.
        rows: All DeviationRows where either quantity or price
            deviation exceeded the threshold.
        n_paper_only: ``client_order_id`` present in self but not
            in ``other``. (paper-only intents)
        n_live_only: ``client_order_id`` present in ``other`` but
            not in self. (live-only intents — possibly missed by
            paper run)
        passed: ``True`` iff ``rows`` is empty AND both ``n_paper_only``
            and ``n_live_only`` are ``0`` (paper and live are identical
            up to the threshold).
    """

    max_deviation_pct: float
    rows: list[DeviationRow] = field(default_factory=list)
    n_paper_only: int = 0
    n_live_only: int = 0

    @property
    def passed(self) -> bool:
        """True iff the comparison meets the CLAUDE.md ≤ 5% target."""
        return (
            not self.rows and self.n_paper_only == 0 and self.n_live_only == 0
        )


# ---------------------------------------------------------------------------
# PaperJournal
# ---------------------------------------------------------------------------


class PaperJournal:
    """SQLite-backed trade journal.

    Args:
        db_path: Path to the SQLite file. Created on first write if
            it does not exist; the schema is initialized on
            construction.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Connection-per-call is simpler than per-instance connection
        # for sqlite3 (avoids "no current transaction" surprises
        # across fork / multiprocess). For Phase 2 multi-thread,
        # wrap with threading.Lock at the runner level.
        with self._connect() as con:
            self._init_schema(con)

    # ---------- connection helper ----------

    def _connect(self) -> sqlite3.Connection:
        # ``isolation_level=None`` enables autocommit-mode for
        # explicit BEGIN/CONTROL — we use the default (deferred)
        # so each method's writes are atomic via context manager.
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
        return con

    def _init_schema(self, con: sqlite3.Connection) -> None:
        cur = con.cursor()
        for stmt in (
            _SCHEMA_ORDER_INTENT,
            _SCHEMA_EXECUTION_REPORT,
            _SCHEMA_FILL,
            _SCHEMA_EQUITY_SNAPSHOT,
            _SCHEMA_META,
        ):
            cur.execute(stmt)
        cur.execute(
            "INSERT OR IGNORE INTO journal_meta (key, value) VALUES (?, ?)",
            ("schema_version", str(JOURNAL_SCHEMA_VERSION)),
        )
        con.commit()

    # ---------- intent ----------

    def record_intent(
        self,
        intent: OrderIntent,
        decision: RiskDecision,
        *,
        bar_timestamp: datetime | None = None,
    ) -> None:
        """Persist an intent + its risk verdict.

        If the same ``client_order_id`` is re-recorded (e.g. on
        resubmission), this UPDATEs the existing row rather than
        failing the PK constraint — the journal always surfaces
        the most recent risk verdict.
        """
        if isinstance(decision, Allow):
            risk_decision = "allow"
            risk_reason = None
        elif isinstance(decision, Reject):
            risk_decision = "reject"
            risk_reason = decision.reason
        else:  # pragma: no cover -- defensive for future decision types
            raise TypeError(f"unsupported risk decision: {type(decision).__name__}")

        # Default ``bar_timestamp`` to UTC now if the runner didn't
        # pass one. The runner always does (so the journal surfaces
        # the bar time, not the record-time) — this fallback just
        # keeps the journal usable from tests.
        if bar_timestamp is None:
            from execution.protocol import utcnow

            bar_timestamp = utcnow()
        ts = bar_timestamp.isoformat()

        with self._connect() as con:
            con.execute(
                """
                INSERT INTO order_intent (
                    client_order_id, bar_timestamp, symbol, side, quantity,
                    price, order_type, reason, risk_decision, risk_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    bar_timestamp = excluded.bar_timestamp,
                    risk_decision = excluded.risk_decision,
                    risk_reason = excluded.risk_reason
                """,
                (
                    intent.client_order_id,
                    ts,
                    intent.symbol,
                    intent.side,
                    intent.quantity,
                    intent.price,
                    intent.order_type,
                    intent.reason,
                    risk_decision,
                    risk_reason,
                ),
            )
            con.commit()

    # ---------- execution report ----------

    def record_report(self, report: ExecutionReport) -> None:
        """Persist an adapter's execution report (UPSERT on client_order_id)."""
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO execution_report (
                    client_order_id, broker_order_id, status,
                    filled_quantity, avg_fill_price, reject_reason, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    broker_order_id = excluded.broker_order_id,
                    status = excluded.status,
                    filled_quantity = excluded.filled_quantity,
                    avg_fill_price = excluded.avg_fill_price,
                    reject_reason = excluded.reject_reason,
                    timestamp = excluded.timestamp
                """,
                (
                    report.client_order_id,
                    report.broker_order_id,
                    report.status,
                    report.filled_quantity,
                    report.avg_fill_price,
                    report.reject_reason,
                    report.timestamp.isoformat() if report.timestamp else None,
                ),
            )
            con.commit()

    # ---------- fill ----------

    def record_fill(self, fill: Fill) -> None:
        """Persist one Fill row."""
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO fill (
                    fill_id, client_order_id, broker_order_id,
                    symbol, side, quantity, price,
                    commission, stamp_tax, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fill_id) DO NOTHING
                """,
                (
                    fill.fill_id,
                    fill.client_order_id,
                    fill.broker_order_id,
                    fill.symbol,
                    fill.side,
                    fill.quantity,
                    fill.price,
                    fill.commission,
                    fill.stamp_tax,
                    fill.timestamp.isoformat(),
                ),
            )
            con.commit()

    # ---------- equity snapshot ----------

    def record_snapshot(self, snap: EquitySnapshot) -> None:
        """Persist one EquitySnapshot row (UPSERT on timestamp)."""
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO equity_snapshot (
                    timestamp, cash, positions_value, total_equity, drawdown_pct
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(timestamp) DO UPDATE SET
                    cash = excluded.cash,
                    positions_value = excluded.positions_value,
                    total_equity = excluded.total_equity,
                    drawdown_pct = excluded.drawdown_pct
                """,
                (
                    snap.timestamp.isoformat(),
                    snap.cash,
                    snap.positions_value,
                    snap.total_equity,
                    snap.drawdown_pct,
                ),
            )
            con.commit()

    # ---------- query API ----------

    def query_intents(
        self, day: date_cls | None = None
    ) -> list[OrderIntent]:
        """Read back OrderIntents, optionally filtered by trade date.

        Args:
            day: If provided, only return intents whose
                ``bar_timestamp`` starts with ``YYYY-MM-DD``.
        """
        where = ""
        params: tuple = ()
        if day is not None:
            where = " WHERE substr(bar_timestamp, 1, 10) = ?"
            params = (day.isoformat(),)
        with self._connect() as con:
            rows = con.execute(
                f"SELECT client_order_id, bar_timestamp, symbol, side, quantity, "
                f"price, order_type, reason FROM order_intent{where} ORDER BY bar_timestamp",
                params,
            ).fetchall()
        return [
            OrderIntent(
                client_order_id=r[0],
                symbol=r[2],
                side=r[3],  # type: ignore[arg-type]
                quantity=r[4],
                price=r[5],
                order_type=r[6],  # type: ignore[arg-type]
                reason=r[7] or "",
            )
            for r in rows
        ]

    def query_fills(self, day: date_cls | None = None) -> list[Fill]:
        """Read back Fill rows, optionally filtered by trade date."""
        where = ""
        params: tuple = ()
        if day is not None:
            where = " WHERE substr(timestamp, 1, 10) = ?"
            params = (day.isoformat(),)
        with self._connect() as con:
            rows = con.execute(
                f"SELECT fill_id, client_order_id, broker_order_id, symbol, side, "
                f"quantity, price, commission, stamp_tax, timestamp FROM fill{where} "
                f"ORDER BY timestamp",
                params,
            ).fetchall()
        return [
            Fill(
                fill_id=r[0],
                client_order_id=r[1],
                broker_order_id=r[2],
                symbol=r[3],
                side=r[4],  # type: ignore[arg-type]
                quantity=r[5],
                price=r[6],
                commission=r[7],
                stamp_tax=r[8],
                timestamp=datetime.fromisoformat(r[9]),
            )
            for r in rows
        ]

    def compute_daily_trade_count(self, day: date_cls) -> int:
        """Count distinct client_order_ids that produced a fill on ``day``.

        A "trade" is one client_order_id that has at least one fill
        on the given day. Round-trip semantics are approximated by
        counting both buy and sell intent ids (the runner is
        responsible for matching buy↔sell pairs into true round-trips
        if a stricter definition is needed later).
        """
        with self._connect() as con:
            row = con.execute(
                """
                SELECT COUNT(DISTINCT client_order_id) FROM fill
                WHERE substr(timestamp, 1, 10) = ?
                """,
                (day.isoformat(),),
            ).fetchone()
        return int(row[0]) if row else 0

    # ---------- compare_to (Phase 2 hook, callable today) ----------

    def compare_to(
        self,
        other: PaperJournal,
        *,
        max_deviation_pct: float = 5.0,
    ) -> CompareResult:
        """Compare this journal to ``other`` on every common client_order_id.

        Used in Phase 2 to validate that the live session deviates
        from the paper session by no more than ``max_deviation_pct``
        (default 5.0, matching CLAUDE.md 「< 5%」).

        Args:
            other: The "reference" journal (in Phase 2: the live
                journal; in tests: a fixture).
            max_deviation_pct: Per-row threshold. Rows where either
                quantity deviation or price deviation exceeds this
                are listed in ``CompareResult.rows``.

        Returns:
            :class:`CompareResult`. ``passed`` is True iff no row
            exceeds the threshold AND both journals have the same
            set of client_order_ids.
        """
        with self._connect() as con_a, other._connect() as con_b:
            a_rows = con_a.execute(
                """
                SELECT f.client_order_id, f.symbol, f.quantity, f.price
                FROM fill f
                """
            ).fetchall()
            b_rows = con_b.execute(
                """
                SELECT f.client_order_id, f.symbol, f.quantity, f.price
                FROM fill f
                """
            ).fetchall()

        a_by_id: dict[str, tuple[str, int, float]] = {
            r[0]: (r[1], int(r[2]), float(r[3])) for r in a_rows
        }
        b_by_id: dict[str, tuple[str, int, float]] = {
            r[0]: (r[1], int(r[2]), float(r[3])) for r in b_rows
        }

        common = set(a_by_id) & set(b_by_id)
        only_a = set(a_by_id) - set(b_by_id)
        only_b = set(b_by_id) - set(a_by_id)

        rows: list[DeviationRow] = []
        for cid in sorted(common):
            a_sym, a_qty, a_price = a_by_id[cid]
            _b_sym, b_qty, b_price = b_by_id[cid]
            qty_dev = abs(a_qty - b_qty) / max(a_qty, 1)
            price_dev = abs(a_price - b_price) / max(a_price, 1e-6)
            if qty_dev * 100.0 >= max_deviation_pct or price_dev * 100.0 >= max_deviation_pct:
                rows.append(
                    DeviationRow(
                        client_order_id=cid,
                        symbol=a_sym,
                        paper_quantity=a_qty,
                        live_quantity=b_qty,
                        paper_price=a_price,
                        live_price=b_price,
                        quantity_deviation_pct=qty_dev * 100.0,
                        price_deviation_pct=price_dev * 100.0,
                    )
                )

        return CompareResult(
            max_deviation_pct=max_deviation_pct,
            rows=rows,
            n_paper_only=len(only_a),
            n_live_only=len(only_b),
        )
