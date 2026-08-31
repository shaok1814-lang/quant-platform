"""Broker adapter registry.

A trivial name → factory mapping. Lets the runner do::

    adapter = create_registered_broker("akquant_paper")
    adapter.connect()

without importing the concrete class. Phase 2 will register
``"xtquant_live"`` here.

Pattern mirrors AKQuant's own ``akquant.gateway.registry``
(``register_broker`` / ``get_broker_builder`` / etc.). We don't
import AKQuant's registry — its API is broader (GatewayBundle,
capabilities, etc.) and we only need a factory-by-name here.
"""

from __future__ import annotations

from collections.abc import Callable

from execution.brokers.base import BrokerAdapter

__all__ = [
    "BrokerRegistry",
    "create_registered_broker",
    "list_registered_brokers",
    "register_broker",
]


# Module-level singleton. Tests can poke at this directly to
# isolate themselves; production code uses the helpers.
_REGISTRY: dict[str, Callable[..., BrokerAdapter]] = {}


def register_broker(name: str, factory: Callable[..., BrokerAdapter]) -> None:
    """Register ``factory(name=...)`` under ``name``.

    Args:
        name: Lookup key (e.g. ``"akquant_paper"``).
        factory: Callable returning a :class:`BrokerAdapter`. May
            take any kwargs; the runner currently passes nothing
            for the default registered factories.
    """
    if name in _REGISTRY:
        # Idempotent re-registration is allowed (idempotent at
        # import time when both sides import the same factory),
        # but a different factory for the same name is a real
        # conflict and we want to know.
        if _REGISTRY[name] is not factory:
            raise ValueError(
                f"broker {name!r} already registered with a different factory"
            )
        return
    _REGISTRY[name] = factory


def create_registered_broker(name: str, **kwargs: object) -> BrokerAdapter:
    """Instantiate the broker registered under ``name``."""
    if name not in _REGISTRY:
        raise KeyError(
            f"broker {name!r} not registered. known: "
            f"{sorted(_REGISTRY.keys())}"
        )
    factory = _REGISTRY[name]
    return factory(**kwargs)


def list_registered_brokers() -> list[str]:
    """Return the sorted list of registered broker names."""
    return sorted(_REGISTRY.keys())


# Alias kept for parity with the import surface promised in
# ``execution.brokers.__init__``. The class is just a thin
# facade over the module-level dict.
class BrokerRegistry:
    """Object-oriented facade over the module-level registry.

    Use this if you want to inject a registry into the runner for
    tests (override ``create_registered_broker``). Most code
    should use the module-level functions directly.
    """

    @staticmethod
    def register(name: str, factory: Callable[..., BrokerAdapter]) -> None:
        register_broker(name, factory)

    @staticmethod
    def create(name: str, **kwargs: object) -> BrokerAdapter:
        return create_registered_broker(name, **kwargs)

    @staticmethod
    def names() -> list[str]:
        return list_registered_brokers()


# ---------------------------------------------------------------------------
# Built-in broker registrations (side-effect imports)
# ---------------------------------------------------------------------------


def _register_builtins() -> None:
    """Register the Phase 1 default brokers.

    Importing is lazy (inside the function) so importing this
    module does NOT pull in AKQuant / xtquant — the runner gets to
    pick which adapter to instantiate.
    """
    if _REGISTRY:
        return  # already registered (idempotent)

    from execution.brokers.akquant_paper import AkquantPaperAdapter
    from execution.brokers.xtquant_live import XtQuantLiveAdapter

    register_broker("akquant_paper", AkquantPaperAdapter)
    register_broker("xtquant_live", XtQuantLiveAdapter)


_register_builtins()
