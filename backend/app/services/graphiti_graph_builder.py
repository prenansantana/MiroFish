"""
Graphiti+Neo4j graph builder.

Mirrors the public surface of `GraphBuilderService` (Zep version) so the
factory can dispatch transparently. Used to build the initial KG from
the user-uploaded PDFs (the path that goes through `api/graph.py`).

Key differences from the Zep flow:
  - `create_graph(name)` does not call any backend: in Graphiti, a
    `group_id` is implicit on the first `add_episode`. We just generate
    an id and return it.
  - `set_ontology(graph_id, ontology)` does not call the backend either:
    Graphiti expects entity_types / edge_types as kwargs on every
    `add_episode` call (rather than registered server-side once). We
    build Pydantic classes from the dict and stash them per-graph.
  - `_wait_for_episodes(uuids)` is a no-op: `Graphiti.add_episode` is
    synchronous from the caller's perspective — once it returns, the
    graph is queryable.
"""

from __future__ import annotations

import threading
import time
import uuid as uuid_lib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from graphiti_core.nodes import EpisodeType
from pydantic import BaseModel, Field, create_model

from ..config import Config
from ..models.task import TaskManager, TaskStatus
from ..utils.locale import get_locale, set_locale, t
from ..utils.logger import get_logger
from ._graphiti_clients import make_graphiti
from ._memory_backend import AsyncRunner

# Reuse the Zep version's GraphInfo dataclass — pure shape, backend-agnostic.
from .graph_builder import GraphInfo  # noqa: F401
from .text_processor import TextProcessor

logger = get_logger('mirofish.graphiti_graph_builder')


# Graphiti reserves these property names internally; renaming user-defined
# attributes that collide keeps the schema clean.
_RESERVED_ATTR_NAMES = {
    'uuid', 'name', 'group_id', 'name_embedding', 'summary', 'created_at',
}


def _safe_attr_name(name: str) -> str:
    if name.lower() in _RESERVED_ATTR_NAMES:
        return f"entity_{name}"
    return name


def _build_pydantic_class(
    base: type[BaseModel],
    name: str,
    description: str,
    attrs: List[Dict[str, Any]],
    attr_type: type = str,
) -> type[BaseModel]:
    """Build a Pydantic class from an ontology dict definition."""
    fields: Dict[str, Any] = {}
    for attr_def in attrs:
        attr_name = _safe_attr_name(attr_def["name"])
        attr_desc = attr_def.get("description") or attr_name
        fields[attr_name] = (
            Optional[attr_type],
            Field(default=None, description=attr_desc),
        )
    cls = create_model(name, __base__=base, **fields)
    cls.__doc__ = description
    return cls


class GraphitiGraphBuilder:
    """Graphiti equivalent of GraphBuilderService.

    Public surface (must match):
      - create_graph(name) -> str
      - set_ontology(graph_id, ontology) -> None
      - add_text_batches(graph_id, chunks, batch_size, progress_callback) -> list[str]
      - _wait_for_episodes(episode_uuids, progress_callback, timeout) -> None
      - get_graph_data(graph_id) -> dict
      - delete_graph(graph_id) -> None
      - build_graph_async(text, ontology, ...) -> task_id
    """

    def __init__(self, *_args, **_kwargs) -> None:
        # Args ignored; kept for signature compat with GraphBuilderService(api_key=...).
        if not Config.NEO4J_PASSWORD:
            raise ValueError("NEO4J_PASSWORD not configured")

        self._graphiti = None
        self._runner = AsyncRunner(name="GraphitiBuilderRunner")
        self._runner_started = False
        self._ontology: Dict[str, Dict[str, Any]] = {}  # graph_id -> {entity_types, edge_types, edge_type_map}
        self.task_manager = TaskManager()

    def _ensure_runner(self) -> AsyncRunner:
        if not self._runner_started:
            self._runner.start()
            self._runner.submit(self._init_graphiti(), timeout=60)
            self._runner_started = True
        return self._runner

    async def _init_graphiti(self) -> None:
        self._graphiti = make_graphiti(
            Config.NEO4J_URI, Config.NEO4J_USER, Config.NEO4J_PASSWORD
        )
        await self._graphiti.build_indices_and_constraints()

    def close(self) -> None:
        if self._graphiti is not None and self._runner_started:
            try:
                self._runner.submit(self._graphiti.close(), timeout=10)
            except Exception as e:
                logger.warning(f"Error closing Graphiti: {e}")
        if self._runner_started:
            self._runner.stop()
            self._runner_started = False

    # ------------------------------------------------------------------
    # Public API (mirrors GraphBuilderService)
    # ------------------------------------------------------------------

    def create_graph(self, name: str) -> str:
        """Generate a new graph_id. Graphiti creates the group implicitly on first episode."""
        graph_id = f"mirofish_{uuid_lib.uuid4().hex[:16]}"
        logger.info(f"Allocated Graphiti group_id={graph_id} (name={name!r})")
        return graph_id

    def set_ontology(self, graph_id: str, ontology: Dict[str, Any]) -> None:
        """Translate the ontology dict into Pydantic classes + edge type map.

        Stored per graph_id and applied on every subsequent add_episode call.
        """
        entity_types: Dict[str, type[BaseModel]] = {}
        for ent in ontology.get("entity_types", []):
            ent_name = ent["name"]
            entity_types[ent_name] = _build_pydantic_class(
                base=BaseModel,
                name=ent_name,
                description=ent.get("description", f"A {ent_name} entity."),
                attrs=ent.get("attributes", []),
                attr_type=str,
            )

        edge_types: Dict[str, type[BaseModel]] = {}
        edge_type_map: Dict[tuple, List[str]] = {}
        for edge in ontology.get("edge_types", []):
            edge_name = edge["name"]
            class_name = ''.join(w.capitalize() for w in edge_name.split('_'))
            edge_types[edge_name] = _build_pydantic_class(
                base=BaseModel,
                name=class_name,
                description=edge.get("description", f"A {edge_name} relationship."),
                attrs=edge.get("attributes", []),
                attr_type=str,
            )
            for st in edge.get("source_targets", []):
                key = (st.get("source", "Entity"), st.get("target", "Entity"))
                edge_type_map.setdefault(key, []).append(edge_name)

        self._ontology[graph_id] = {
            "entity_types": entity_types or None,
            "edge_types": edge_types or None,
            "edge_type_map": edge_type_map or None,
        }
        logger.info(
            f"Ontology stored for {graph_id}: "
            f"{len(entity_types)} entity types, {len(edge_types)} edge types, "
            f"{len(edge_type_map)} edge type mappings"
        )

    # Concurrency for parallel add_episode calls. Anthropic's free/dev tier
    # tolerates ~5 concurrent requests without rate-limiting.
    PARALLEL_EPISODES = 5

    # First N chunks run sequentially so core entities (the recurring
    # named people, organizations, places) get materialised + indexed
    # before parallel chunks try to deduplicate against them. Without
    # this, two parallel chunks can each independently create their own
    # node for the same entity before either has committed — Graphiti's
    # resolve_extracted_nodes can't dedupe what's not in the DB yet.
    # After warmup, parallel chunks dedupe against the existing entities
    # and only create genuinely new ones.
    WARMUP_SEQUENTIAL_CHUNKS = 8

    def add_text_batches(
        self,
        graph_id: str,
        chunks: List[str],
        batch_size: int = 3,
        progress_callback: Optional[Callable[[str, float], None]] = None,
    ) -> List[str]:
        """Add chunks to the graph N at a time via asyncio.gather.

        `batch_size` here means "how many chunks to fan out concurrently",
        not the inverse of progress reporting (Graphiti has no native batch
        API). We override callers' batch_size with PARALLEL_EPISODES so the
        old behaviour (sequential, 1 at a time) becomes parallel.
        """
        self._ensure_runner()
        ontology = self._ontology.get(graph_id, {})
        episode_uuids: List[str] = []
        total = len(chunks)
        parallel = max(1, self.PARALLEL_EPISODES)
        warmup = min(self.WARMUP_SEQUENTIAL_CHUNKS, total)

        # Phase 1 — warm-up: process first chunks sequentially so core
        # entities are committed before any parallel batch tries to dedupe.
        for i in range(warmup):
            try:
                ep_uuid = self._runner.submit(
                    self._add_episode_async(
                        graph_id=graph_id,
                        name=f"chunk-{i}",
                        body=chunks[i],
                        ontology=ontology,
                    ),
                    timeout=600,
                )
                if ep_uuid:
                    episode_uuids.append(ep_uuid)
            except Exception as e:
                if progress_callback:
                    progress_callback(
                        t('progress.batchFailed', batch=i + 1, error=str(e)),
                        i / max(total, 1),
                    )
                raise
            if progress_callback:
                progress_callback(
                    t(
                        'progress.sendingBatch',
                        current=i + 1,
                        total=total,
                        chunks=1,
                    ),
                    (i + 1) / max(total, 1),
                )

        # Phase 2 — parallel: remaining chunks fan out PARALLEL_EPISODES at
        # a time. They dedupe against the entities materialised in phase 1
        # of the warm-up. After all batches finish, we run an auto-dedup
        # pass to catch the rare race where two parallel chunks each
        # created their own node for the same entity before either had
        # committed.
        for batch_start in range(warmup, total, parallel):
            batch = chunks[batch_start:batch_start + parallel]
            batch_num = (batch_start - warmup) // parallel + 1
            remaining_batches = (total - warmup + parallel - 1) // parallel
            try:
                results = self._runner.submit(
                    self._add_episodes_parallel(
                        graph_id=graph_id,
                        chunks=batch,
                        start_index=batch_start,
                        ontology=ontology,
                    ),
                    timeout=600,
                )
                for r in results:
                    if r:
                        episode_uuids.append(r)
            except Exception as e:
                if progress_callback:
                    progress_callback(
                        t('progress.batchFailed', batch=batch_num, error=str(e)),
                        batch_start / max(total, 1),
                    )
                raise

            if progress_callback:
                progress_callback(
                    t(
                        'progress.sendingBatch',
                        current=warmup + batch_num,
                        total=warmup + remaining_batches,
                        chunks=len(batch),
                    ),
                    min(batch_start + len(batch), total) / max(total, 1),
                )

        # Phase 3 — auto-dedup. Catches duplicates that slipped past the
        # in-pipeline resolve_extracted_nodes due to parallel commits.
        self._auto_dedupe(graph_id)

        return episode_uuids

    def _auto_dedupe(self, graph_id: str) -> None:
        """Merge any exact-name duplicates created by parallel chunks.

        Runs synchronously after add_text_batches completes. Phase 1 only
        — exact name + same custom label inside the same group_id. Phase
        2 (short-form vs long-form alias candidates) is never
        auto-applied because shared tokens can be coincidence.
        """
        from neo4j import GraphDatabase
        from ._graphiti_dedupe import auto_merge_exact_duplicates

        try:
            with GraphDatabase.driver(
                Config.NEO4J_URI,
                auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD),
            ) as driver:
                auto_merge_exact_duplicates(driver, graph_id)
        except Exception as e:
            logger.warning(f"Auto-dedup skipped for {graph_id}: {e}")

    async def _add_episodes_parallel(
        self,
        graph_id: str,
        chunks: List[str],
        start_index: int,
        ontology: Dict[str, Any],
    ) -> List[Optional[str]]:
        import asyncio
        coros = [
            self._add_episode_async(
                graph_id=graph_id,
                name=f"chunk-{start_index + i}",
                body=chunk,
                ontology=ontology,
            )
            for i, chunk in enumerate(chunks)
        ]
        return await asyncio.gather(*coros, return_exceptions=False)

    async def _add_episode_async(
        self,
        graph_id: str,
        name: str,
        body: str,
        ontology: Dict[str, Any],
    ) -> Optional[str]:
        assert self._graphiti is not None, "Graphiti not initialized"
        result = await self._graphiti.add_episode(
            name=name,
            episode_body=body,
            source=EpisodeType.text,
            source_description='document_chunk',
            reference_time=datetime.now(timezone.utc),
            group_id=graph_id,
            entity_types=ontology.get("entity_types"),
            edge_types=ontology.get("edge_types"),
            edge_type_map=ontology.get("edge_type_map"),
        )
        episode = getattr(result, 'episode', None) or result
        return getattr(episode, 'uuid', None)

    def _wait_for_episodes(
        self,
        episode_uuids: List[str],
        progress_callback: Optional[Callable[[str, float], None]] = None,
        timeout: int = 600,
    ) -> None:
        """No-op for Graphiti — add_episode is synchronous from the caller's POV."""
        if progress_callback:
            progress_callback(
                t('progress.processingComplete', completed=len(episode_uuids), total=len(episode_uuids)),
                1.0,
            )

    def get_graph_data(self, graph_id: str) -> Dict[str, Any]:
        """Read nodes and edges back via Cypher (uses GraphitiEntityReader)."""
        # Lazy import to avoid circular dep
        from .graphiti_entity_reader import GraphitiEntityReader

        reader = GraphitiEntityReader()
        try:
            nodes = reader.get_all_nodes(graph_id)
            edges = reader.get_all_edges(graph_id)
        finally:
            reader.close()

        def _serialize(value):
            """Recursively convert neo4j DateTime / non-JSON values to strings."""
            if value is None:
                return None
            if isinstance(value, (str, int, float, bool)):
                return value
            if isinstance(value, list):
                return [_serialize(v) for v in value]
            if isinstance(value, dict):
                return {k: _serialize(v) for k, v in value.items()}
            if hasattr(value, 'isoformat'):
                return value.isoformat()
            return str(value)

        node_map = {n["uuid"]: n.get("name", "") for n in nodes}
        nodes_data = []
        for n in nodes:
            attrs = _serialize(n.get("attributes", {}))
            nodes_data.append({
                "uuid": n["uuid"],
                "name": n.get("name", ""),
                "labels": n.get("labels", []),
                "summary": n.get("summary", ""),
                "attributes": attrs,
                "created_at": attrs.get("created_at") if isinstance(attrs, dict) else None,
            })
        edges_data = []
        for e in edges:
            attrs = _serialize(e.get("attributes", {}))
            attrs_dict = attrs if isinstance(attrs, dict) else {}
            edges_data.append(
                {
                    "uuid": e.get("uuid", ""),
                    "name": e.get("name", ""),
                    "fact": e.get("fact", ""),
                    "fact_type": e.get("name", ""),
                    "source_node_uuid": e.get("source_node_uuid", ""),
                    "target_node_uuid": e.get("target_node_uuid", ""),
                    "source_node_name": node_map.get(e.get("source_node_uuid", ""), ""),
                    "target_node_name": node_map.get(e.get("target_node_uuid", ""), ""),
                    "attributes": attrs,
                    "created_at": attrs_dict.get("created_at"),
                    "valid_at": attrs_dict.get("valid_at"),
                    "invalid_at": attrs_dict.get("invalid_at"),
                    "expired_at": attrs_dict.get("expired_at"),
                    "episodes": attrs_dict.get("episodes", []),
                }
            )

        return {
            "graph_id": graph_id,
            "nodes": nodes_data,
            "edges": edges_data,
            "node_count": len(nodes_data),
            "edge_count": len(edges_data),
        }

    def delete_graph(self, graph_id: str) -> None:
        """Delete every node/edge tagged with this group_id."""
        from neo4j import GraphDatabase
        with GraphDatabase.driver(
            Config.NEO4J_URI, auth=(Config.NEO4J_USER, Config.NEO4J_PASSWORD)
        ) as driver:
            with driver.session() as s:
                s.run(
                    "MATCH (n) WHERE n.group_id = $gid DETACH DELETE n",
                    gid=graph_id,
                )
        self._ontology.pop(graph_id, None)
        logger.info(f"Deleted graph_id={graph_id}")

    # ------------------------------------------------------------------
    # build_graph_async — same orchestration as the Zep version, lifted
    # ------------------------------------------------------------------

    def build_graph_async(
        self,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str = "MiroFish Graph",
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        batch_size: int = 3,
    ) -> str:
        task_id = self.task_manager.create_task(
            task_type="graph_build",
            metadata={
                "graph_name": graph_name,
                "chunk_size": chunk_size,
                "text_length": len(text),
            },
        )

        current_locale = get_locale()
        thread = threading.Thread(
            target=self._build_graph_worker,
            args=(task_id, text, ontology, graph_name, chunk_size, chunk_overlap, batch_size, current_locale),
            daemon=True,
        )
        thread.start()
        return task_id

    def _build_graph_worker(
        self,
        task_id: str,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str,
        chunk_size: int,
        chunk_overlap: int,
        batch_size: int,
        locale: str = 'zh',
    ) -> None:
        set_locale(locale)
        try:
            self.task_manager.update_task(
                task_id,
                status=TaskStatus.PROCESSING,
                progress=5,
                message=t('progress.startBuildingGraph'),
            )

            graph_id = self.create_graph(graph_name)
            self.task_manager.update_task(
                task_id,
                progress=10,
                message=t('progress.graphCreated', graphId=graph_id),
            )

            self.set_ontology(graph_id, ontology)
            self.task_manager.update_task(
                task_id,
                progress=15,
                message=t('progress.ontologySet'),
            )

            chunks = TextProcessor.split_text(text, chunk_size, chunk_overlap)
            total_chunks = len(chunks)
            self.task_manager.update_task(
                task_id,
                progress=20,
                message=t('progress.textSplit', count=total_chunks),
            )

            episode_uuids = self.add_text_batches(
                graph_id,
                chunks,
                batch_size,
                lambda msg, prog: self.task_manager.update_task(
                    task_id,
                    progress=20 + int(prog * 0.7),  # 20-90%
                    message=msg,
                ),
            )

            self.task_manager.update_task(
                task_id,
                progress=90,
                message=t('progress.fetchingGraphInfo'),
            )

            graph_data = self.get_graph_data(graph_id)
            graph_info = GraphInfo(
                graph_id=graph_id,
                node_count=graph_data["node_count"],
                edge_count=graph_data["edge_count"],
                entity_types=list({
                    label
                    for node in graph_data["nodes"]
                    for label in node.get("labels", [])
                    if label not in ("Entity", "Node")
                }),
            )

            self.task_manager.complete_task(task_id, {
                "graph_id": graph_id,
                "graph_info": graph_info.to_dict(),
                "chunks_processed": total_chunks,
            })
        except Exception as e:
            import traceback
            error_msg = f"{e}\n{traceback.format_exc()}"
            self.task_manager.fail_task(task_id, error_msg)
