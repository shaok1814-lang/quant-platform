"""ops — operational layer (W6 P3 M4/M5).

Modules:

* :mod:`ops.universe` — load + validate ``config/universe.yaml``.
* :mod:`ops.quality` — post-ingest OHLCV data quality checks.
* :mod:`ops.notify` — 钉钉 webhook wrapper (default inactive).
* :mod:`ops.ingest_job` — incremental daily ingestion pipeline.
* :mod:`ops.scheduler` — APScheduler launcher that runs the ingest
  job on a configurable daily schedule.

Each module exposes a narrow public API; ``ops.__init__`` only re-exports
the handful of functions callers outside ``ops/`` need.
"""

from __future__ import annotations

from ops.universe import UniverseEntry, load_universe

__all__ = ["UniverseEntry", "load_universe"]
