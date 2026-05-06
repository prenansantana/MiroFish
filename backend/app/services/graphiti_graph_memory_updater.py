"""
Graphiti+Neo4j memory updater.

Mirrors `zep_graph_memory_updater.py` so the factory at `_memory_backend.py`
can swap backends without touching call sites.

Reuse principle: queue/buffer/retry/locale-handling mechanics are copied
1:1 from the Zep version (`zep_graph_memory_updater.py:202-477`), because
that logic is backend-agnostic. Only `_send_batch_activities` is replaced
with a Graphiti `add_episode` call.

Async bridging: Graphiti's API is fully async. Each updater owns a
dedicated `AsyncRunner` (asyncio loop in a daemon thread); the worker
thread submits `graphiti.add_episode(...)` coroutines onto it.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Any, Dict, List, Optional

from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType

from ..config import Config
from ..utils.locale import get_locale, set_locale
from ..utils.logger import get_logger
from ._graphiti_clients import make_graphiti
from ._memory_backend import AsyncRunner

# AgentActivity is reused as-is from the Zep updater.
from .zep_graph_memory_updater import AgentActivity  # noqa: F401  (re-exported)

logger = get_logger('mirofish.graphiti_graph_memory_updater')


class GraphitiGraphMemoryUpdater:
    """Graphiti equivalent of ZepGraphMemoryUpdater. Same public surface."""

    BATCH_SIZE = 5
    SEND_INTERVAL = 0.5
    MAX_RETRIES = 3                  # generic errors: keep the original 3 attempts
    RETRY_DELAY = 2                  # seconds, multiplied by attempt index
    MAX_RATE_LIMIT_RETRIES = 10      # rate limit (429): retry up to 10 times
    RATE_LIMIT_DEFAULT_BACKOFF_S = 60  # default wait if no retry-after info
    RATE_LIMIT_BACKOFF_CAP_S = 120   # never wait longer than this per attempt

    PLATFORM_DISPLAY_NAMES = {
        'twitter': '世界1',
        'reddit': '世界2',
    }

    def __init__(
        self,
        graph_id: str,
        neo4j_uri: Optional[str] = None,
        neo4j_user: Optional[str] = None,
        neo4j_password: Optional[str] = None,
    ) -> None:
        self.graph_id = graph_id  # Mapped to Graphiti's group_id
        self.neo4j_uri = neo4j_uri or Config.NEO4J_URI
        self.neo4j_user = neo4j_user or Config.NEO4J_USER
        self.neo4j_password = neo4j_password or Config.NEO4J_PASSWORD

        if not self.neo4j_password:
            raise ValueError("NEO4J_PASSWORD not configured")

        self._graphiti: Optional[Graphiti] = None
        self._runner = AsyncRunner(name=f"GraphitiUpdater-{graph_id[:8]}")

        self._activity_queue: Queue = Queue()
        self._platform_buffers: Dict[str, List[AgentActivity]] = {
            'twitter': [],
            'reddit': [],
        }
        self._buffer_lock = threading.Lock()

        self._running = False
        self._worker_thread: Optional[threading.Thread] = None

        self._total_activities = 0
        self._total_sent = 0
        self._total_items_sent = 0
        self._failed_count = 0
        self._skipped_count = 0

        logger.info(
            f"GraphitiGraphMemoryUpdater initialized: graph_id={graph_id}, "
            f"batch_size={self.BATCH_SIZE}"
        )

    def _get_platform_display_name(self, platform: str) -> str:
        return self.PLATFORM_DISPLAY_NAMES.get(platform.lower(), platform)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return

        self._runner.start()
        self._runner.submit(self._init_graphiti(), timeout=60)

        current_locale = get_locale()
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            args=(current_locale,),
            daemon=True,
            name=f"GraphitiUpdaterWorker-{self.graph_id[:8]}",
        )
        self._worker_thread.start()
        logger.info(f"GraphitiGraphMemoryUpdater started: graph_id={self.graph_id}")

    def stop(self) -> None:
        self._running = False
        self._flush_remaining()

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=10)

        if self._graphiti is not None:
            try:
                self._runner.submit(self._close_graphiti(), timeout=10)
            except Exception as e:
                logger.warning(f"Error closing Graphiti client: {e}")

        self._runner.stop()

        logger.info(
            f"GraphitiGraphMemoryUpdater stopped: graph_id={self.graph_id}, "
            f"total_activities={self._total_activities}, "
            f"batches_sent={self._total_sent}, "
            f"items_sent={self._total_items_sent}, "
            f"failed={self._failed_count}, "
            f"skipped={self._skipped_count}"
        )

    # ------------------------------------------------------------------
    # Public ingestion API (matches Zep updater)
    # ------------------------------------------------------------------

    def add_activity(self, activity: AgentActivity) -> None:
        if activity.action_type == "DO_NOTHING":
            self._skipped_count += 1
            return
        self._activity_queue.put(activity)
        self._total_activities += 1
        logger.debug(
            f"Added activity to Graphiti queue: "
            f"{activity.agent_name} - {activity.action_type}"
        )

    def add_activity_from_dict(self, data: Dict[str, Any], platform: str) -> None:
        if "event_type" in data:
            return
        activity = AgentActivity(
            platform=platform,
            agent_id=data.get("agent_id", 0),
            agent_name=data.get("agent_name", ""),
            action_type=data.get("action_type", ""),
            action_args=data.get("action_args", {}),
            round_num=data.get("round", 0),
            timestamp=data.get("timestamp", datetime.now().isoformat()),
        )
        self.add_activity(activity)

    def get_stats(self) -> Dict[str, Any]:
        with self._buffer_lock:
            buffer_sizes = {p: len(b) for p, b in self._platform_buffers.items()}
        return {
            "graph_id": self.graph_id,
            "batch_size": self.BATCH_SIZE,
            "total_activities": self._total_activities,
            "batches_sent": self._total_sent,
            "items_sent": self._total_items_sent,
            "failed_count": self._failed_count,
            "skipped_count": self._skipped_count,
            "queue_size": self._activity_queue.qsize(),
            "buffer_sizes": buffer_sizes,
            "running": self._running,
        }

    # ------------------------------------------------------------------
    # Async Graphiti operations (run on AsyncRunner's loop)
    # ------------------------------------------------------------------

    async def _init_graphiti(self) -> None:
        self._graphiti = make_graphiti(
            self.neo4j_uri, self.neo4j_user, self.neo4j_password
        )
        # Idempotent — safe to call across many updaters.
        await self._graphiti.build_indices_and_constraints()

    async def _close_graphiti(self) -> None:
        if self._graphiti is not None:
            await self._graphiti.close()
            self._graphiti = None

    async def _add_episode_async(
        self, name: str, body: str, ref_time: datetime
    ) -> None:
        assert self._graphiti is not None, "Graphiti not initialized"
        await self._graphiti.add_episode(
            name=name,
            episode_body=body,
            source=EpisodeType.text,
            source_description='oasis_simulation',
            reference_time=ref_time,
            group_id=self.graph_id,
        )

    # ------------------------------------------------------------------
    # Worker loop (sync, mirrors Zep version)
    # ------------------------------------------------------------------

    def _worker_loop(self, locale: str = 'zh') -> None:
        set_locale(locale)
        while self._running or not self._activity_queue.empty():
            try:
                try:
                    activity = self._activity_queue.get(timeout=1)
                    platform = activity.platform.lower()
                    with self._buffer_lock:
                        if platform not in self._platform_buffers:
                            self._platform_buffers[platform] = []
                        self._platform_buffers[platform].append(activity)

                        if len(self._platform_buffers[platform]) >= self.BATCH_SIZE:
                            batch = self._platform_buffers[platform][: self.BATCH_SIZE]
                            self._platform_buffers[platform] = self._platform_buffers[platform][
                                self.BATCH_SIZE:
                            ]
                            self._send_batch_activities(batch, platform)
                            time.sleep(self.SEND_INTERVAL)
                except Empty:
                    pass
            except Exception as e:
                logger.error(f"Worker loop exception: {e}")
                time.sleep(1)

    def _send_batch_activities(
        self, activities: List[AgentActivity], platform: str
    ) -> None:
        if not activities:
            return

        episode_texts = [a.to_episode_text() for a in activities]
        combined_text = "\n".join(episode_texts)
        episode_name = (
            f"{platform}-batch-{activities[0].round_num}-"
            f"{datetime.now(timezone.utc).isoformat()}"
        )
        ref_time = datetime.now(timezone.utc)

        # Two independent retry budgets:
        #   - generic_attempts:  errors that aren't rate-limit (network, neo4j,
        #     timeouts) — fast linear backoff, hard cap MAX_RETRIES
        #   - rate_limit_attempts:  Anthropic 429s — long backoff respecting
        #     retry-after header, hard cap MAX_RATE_LIMIT_RETRIES
        # Mixing them in one counter would either drop episodes too eagerly
        # on rate limits, or hammer the API on transient failures.
        generic_attempts = 0
        rate_limit_attempts = 0
        display_name = self._get_platform_display_name(platform)

        while True:
            try:
                self._runner.submit(
                    self._add_episode_async(
                        name=episode_name, body=combined_text, ref_time=ref_time
                    ),
                    timeout=180,
                )
                self._total_sent += 1
                self._total_items_sent += len(activities)
                logger.info(
                    f"Sent {len(activities)} {display_name} activities to "
                    f"Graphiti graph {self.graph_id}"
                )
                logger.debug(f"Batch preview: {combined_text[:200]}...")
                return
            except Exception as e:
                if self._is_rate_limit_error(e):
                    rate_limit_attempts += 1
                    if rate_limit_attempts >= self.MAX_RATE_LIMIT_RETRIES:
                        logger.error(
                            f"Graphiti batch send dropped after "
                            f"{rate_limit_attempts} rate-limit retries: {e}"
                        )
                        self._failed_count += 1
                        return
                    wait_s = self._rate_limit_wait_seconds(e)
                    logger.warning(
                        f"Anthropic rate limit on Graphiti batch "
                        f"({len(activities)} {display_name} activities) — "
                        f"waiting {wait_s}s before retry "
                        f"({rate_limit_attempts}/{self.MAX_RATE_LIMIT_RETRIES})"
                    )
                    time.sleep(wait_s)
                    continue

                generic_attempts += 1
                if generic_attempts >= self.MAX_RETRIES:
                    logger.error(
                        f"Graphiti batch send failed after {generic_attempts} "
                        f"attempts: {e}"
                    )
                    self._failed_count += 1
                    return
                logger.warning(
                    f"Graphiti batch send failed (attempt "
                    f"{generic_attempts}/{self.MAX_RETRIES}): {e}"
                )
                time.sleep(self.RETRY_DELAY * generic_attempts)

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        """True for Anthropic-style rate-limit errors. Matches the SDK's
        explicit 429 exceptions plus the textual marker that comes through
        when the error is re-raised from a deeper layer (Graphiti wraps
        SDK errors before we see them)."""
        # Anthropic SDK's RateLimitError subclasses APIStatusError; checking
        # status_code is the cleanest signal but the exception type may be
        # opaque after Graphiti's wrappers. Fall back to substring match.
        status = getattr(exc, 'status_code', None)
        if status == 429:
            return True
        msg = str(exc).lower()
        return ('rate_limit_error' in msg
                or 'rate limit exceeded' in msg
                or 'rate-limit' in msg
                or '429' in msg)

    def _rate_limit_wait_seconds(self, exc: Exception) -> int:
        """Extract retry-after seconds from the response if present, otherwise
        fall back to RATE_LIMIT_DEFAULT_BACKOFF_S. Capped at
        RATE_LIMIT_BACKOFF_CAP_S so a misconfigured upstream can't park us
        for hours."""
        # Anthropic SDK exceptions expose response headers under .response
        try:
            resp = getattr(exc, 'response', None)
            if resp is not None:
                headers = getattr(resp, 'headers', None) or {}
                # Common headers, order matters
                for h in ('retry-after', 'x-ratelimit-reset',
                          'anthropic-ratelimit-input-tokens-reset'):
                    val = headers.get(h)
                    if val is None:
                        continue
                    try:
                        n = int(float(val))
                        return max(1, min(n, self.RATE_LIMIT_BACKOFF_CAP_S))
                    except (TypeError, ValueError):
                        continue
        except Exception:
            pass
        return self.RATE_LIMIT_DEFAULT_BACKOFF_S

    def _flush_remaining(self) -> None:
        while not self._activity_queue.empty():
            try:
                activity = self._activity_queue.get_nowait()
                platform = activity.platform.lower()
                with self._buffer_lock:
                    if platform not in self._platform_buffers:
                        self._platform_buffers[platform] = []
                    self._platform_buffers[platform].append(activity)
            except Empty:
                break

        with self._buffer_lock:
            for platform, buffer in self._platform_buffers.items():
                if buffer:
                    display_name = self._get_platform_display_name(platform)
                    logger.info(
                        f"Flushing {len(buffer)} remaining {display_name} activities"
                    )
                    self._send_batch_activities(buffer, platform)
            for platform in self._platform_buffers:
                self._platform_buffers[platform] = []


class GraphitiGraphMemoryManager:
    """Graphiti equivalent of ZepGraphMemoryManager. Same classmethod surface."""

    _updaters: Dict[str, GraphitiGraphMemoryUpdater] = {}
    _lock = threading.Lock()
    _stop_all_done = False

    @classmethod
    def create_updater(
        cls, simulation_id: str, graph_id: str
    ) -> GraphitiGraphMemoryUpdater:
        with cls._lock:
            if simulation_id in cls._updaters:
                cls._updaters[simulation_id].stop()

            updater = GraphitiGraphMemoryUpdater(graph_id)
            updater.start()
            cls._updaters[simulation_id] = updater

            logger.info(
                f"Created Graphiti memory updater: "
                f"simulation_id={simulation_id}, graph_id={graph_id}"
            )
            return updater

    @classmethod
    def get_updater(cls, simulation_id: str) -> Optional[GraphitiGraphMemoryUpdater]:
        return cls._updaters.get(simulation_id)

    @classmethod
    def stop_updater(cls, simulation_id: str) -> None:
        with cls._lock:
            if simulation_id in cls._updaters:
                cls._updaters[simulation_id].stop()
                del cls._updaters[simulation_id]
                logger.info(
                    f"Stopped Graphiti memory updater: simulation_id={simulation_id}"
                )

    @classmethod
    def stop_all(cls) -> None:
        if cls._stop_all_done:
            return
        cls._stop_all_done = True

        with cls._lock:
            if cls._updaters:
                for sim_id, updater in list(cls._updaters.items()):
                    try:
                        updater.stop()
                    except Exception as e:
                        logger.error(
                            f"Error stopping Graphiti updater "
                            f"simulation_id={sim_id}: {e}"
                        )
                cls._updaters.clear()
            logger.info("All Graphiti memory updaters stopped")

    @classmethod
    def get_all_stats(cls) -> Dict[str, Dict[str, Any]]:
        return {sid: u.get_stats() for sid, u in cls._updaters.items()}
