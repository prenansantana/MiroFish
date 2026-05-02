"""
Memory backend factory.

Selects between Zep Cloud and Graphiti+Neo4j based on Config.MEMORY_BACKEND.

Public surface — call these from Flask handlers and other services
instead of importing ZepToolsService / ZepEntityReader / ZepGraphMemoryUpdater
directly:

    from app.services._memory_backend import (
        get_tools_service,
        get_entity_reader,
        get_memory_updater_manager,
    )

The factory returns instances whose public methods match the existing
Zep service surface, so call sites do not need to know which backend
is active.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine, TypeVar

from ..config import Config
from ..utils.logger import get_logger

logger = get_logger('mirofish.memory_backend')

T = TypeVar('T')


def _is_graphiti() -> bool:
    return Config.MEMORY_BACKEND == 'graphiti'


def get_tools_service(*args, **kwargs):
    """
    Return the active backend's tools service.

    Zep returns ZepToolsService; Graphiti returns GraphitiToolsService.
    Both expose the same public surface (search_graph, get_all_nodes,
    get_all_edges, get_node_detail, get_node_edges, get_entities_by_type,
    get_entity_summary, get_graph_statistics, insight_forge, panorama_search,
    quick_search, interview_agents).
    """
    if _is_graphiti():
        from .graphiti_tools import GraphitiToolsService
        return GraphitiToolsService(*args, **kwargs)
    from .zep_tools import ZepToolsService
    return ZepToolsService(*args, **kwargs)


def get_entity_reader(*args, **kwargs):
    """
    Return the active backend's entity reader.

    Both backends expose: get_all_nodes, get_all_edges, get_node_edges,
    filter_defined_entities, get_entity_with_context, get_entities_by_type.
    """
    if _is_graphiti():
        from .graphiti_entity_reader import GraphitiEntityReader
        return GraphitiEntityReader(*args, **kwargs)
    from .zep_entity_reader import ZepEntityReader
    return ZepEntityReader(*args, **kwargs)


def get_memory_updater_manager():
    """
    Return the active backend's updater manager class (not instance).

    Both classes expose the same classmethod surface:
      - create_updater(simulation_id, graph_id) -> updater instance
      - get_updater(simulation_id) -> updater | None
      - stop_updater(simulation_id)
      - stop_all()
      - get_all_stats() -> dict
    """
    if _is_graphiti():
        from .graphiti_graph_memory_updater import GraphitiGraphMemoryManager
        return GraphitiGraphMemoryManager
    from .zep_graph_memory_updater import ZepGraphMemoryManager
    return ZepGraphMemoryManager


# ---------------------------------------------------------------------------
# Async bridging helpers (used internally by Graphiti adapters).
#
# Graphiti's Python API is fully async. The MiroFish codebase is sync. We
# bridge with two strategies:
#
# 1. AsyncRunner: a long-lived event loop running in a dedicated daemon
#    thread. Use for hot paths (memory updater worker) where creating a
#    fresh loop per call would be wasteful.
#
# 2. run_coro(): one-shot asyncio.run() wrapper. Use for ad-hoc calls
#    from Flask request handlers — simpler, slightly slower per call.
# ---------------------------------------------------------------------------


def run_coro(coro: Coroutine[Any, Any, T]) -> T:
    """
    Synchronously run a coroutine to completion in a fresh event loop.

    Convenient for occasional calls (e.g. Flask handlers). Do NOT use in
    a hot loop — prefer AsyncRunner for that.
    """
    return asyncio.run(coro)


class AsyncRunner:
    """
    Long-lived asyncio event loop running in a dedicated thread.

    Lets sync code submit coroutines and block for the result without
    paying the cost of `asyncio.run()` per call.

    Usage:
        runner = AsyncRunner()
        runner.start()
        result = runner.submit(some_coro())
        runner.stop()
    """

    def __init__(self, name: str = "AsyncRunner") -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._name = name
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop, name=self._name, daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def submit(self, coro: Coroutine[Any, Any, T], timeout: float | None = None) -> T:
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("AsyncRunner not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def stop(self) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._loop = None
        self._thread = None
        self._ready.clear()
