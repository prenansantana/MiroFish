"""
Graphiti+Neo4j entity reader.

Mirrors `zep_entity_reader.py:71-435`. Public surface is identical so
the factory in `_memory_backend.py` can swap implementations transparently.

Graphiti stores entities with `:Entity` label (plus custom-type labels)
and relations as `:RELATES_TO` edges. Multi-tenancy is via the
`group_id` property — we map Zep's `graph_id` to it 1:1.

Pagination is plain Cypher SKIP/LIMIT; max nodes is capped at 2000 to
match the Zep version's `_MAX_NODES`.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, TypeVar

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, TransientError

from ..config import Config
from ..utils.logger import get_logger

# Reuse the dataclasses — they describe shapes, not behavior.
from .zep_entity_reader import EntityNode, FilteredEntities  # noqa: F401

logger = get_logger('mirofish.graphiti_entity_reader')

T = TypeVar('T')

_PAGE_SIZE = 100
_MAX_NODES = 2000
_MAX_RETRIES = 3
_RETRY_DELAY = 2.0


class GraphitiEntityReader:
    """Graphiti equivalent of ZepEntityReader."""

    def __init__(
        self,
        neo4j_uri: Optional[str] = None,
        neo4j_user: Optional[str] = None,
        neo4j_password: Optional[str] = None,
    ) -> None:
        self.neo4j_uri = neo4j_uri or Config.NEO4J_URI
        self.neo4j_user = neo4j_user or Config.NEO4J_USER
        self.neo4j_password = neo4j_password or Config.NEO4J_PASSWORD

        if not self.neo4j_password:
            raise ValueError("NEO4J_PASSWORD not configured")

        self._driver = None  # type: ignore[assignment]

    def _get_driver(self):
        if self._driver is None:
            self._driver = GraphDatabase.driver(
                self.neo4j_uri, auth=(self.neo4j_user, self.neo4j_password)
            )
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _call_with_retry(
        self,
        func: Callable[[], T],
        operation_name: str,
        max_retries: int = _MAX_RETRIES,
    ) -> T:
        last_exc: Optional[BaseException] = None
        delay = _RETRY_DELAY
        for attempt in range(max_retries):
            try:
                return func()
            except (ServiceUnavailable, TransientError, ConnectionError, TimeoutError) as e:
                last_exc = e
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Neo4j {operation_name} attempt {attempt + 1} failed: "
                        f"{str(e)[:100]}, retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)
                    delay *= 2
                else:
                    logger.error(
                        f"Neo4j {operation_name} failed after {max_retries} attempts: {e}"
                    )
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------
    # Read API (mirrors Zep)
    # ------------------------------------------------------------------

    def get_all_nodes(self, graph_id: str) -> List[Dict[str, Any]]:
        """Fetch all entity nodes for a graph (paginated SKIP/LIMIT, capped at _MAX_NODES)."""
        logger.info(f"Fetching all entity nodes for graph {graph_id}...")

        nodes: List[Dict[str, Any]] = []
        skip = 0
        cypher = (
            "MATCH (n:Entity) "
            "WHERE n.group_id = $gid "
            "RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels, "
            "       n.summary AS summary, properties(n) AS attributes "
            "ORDER BY n.created_at "
            "SKIP $skip LIMIT $limit"
        )

        driver = self._get_driver()
        while True:
            page_skip = skip
            records = self._call_with_retry(
                lambda s=page_skip: self._run_read(
                    driver, cypher, gid=graph_id, skip=s, limit=_PAGE_SIZE
                ),
                operation_name=f"fetch nodes (graph={graph_id}, skip={page_skip})",
            )
            if not records:
                break

            for rec in records:
                attrs = dict(rec["attributes"] or {})
                # Strip core cols out of "attributes" to match Zep shape.
                for k in ("uuid", "name", "summary", "group_id", "labels", "created_at"):
                    attrs.pop(k, None)
                nodes.append(
                    {
                        "uuid": rec["uuid"] or "",
                        "name": rec["name"] or "",
                        "labels": list(rec["labels"] or []),
                        "summary": rec["summary"] or "",
                        "attributes": attrs,
                    }
                )

            if len(nodes) >= _MAX_NODES:
                logger.warning(
                    f"Node count reached limit ({_MAX_NODES}), stopping pagination "
                    f"for graph {graph_id}"
                )
                nodes = nodes[:_MAX_NODES]
                break
            if len(records) < _PAGE_SIZE:
                break
            skip += _PAGE_SIZE

        logger.info(f"Fetched {len(nodes)} nodes")
        return nodes

    def get_all_edges(self, graph_id: str) -> List[Dict[str, Any]]:
        """Fetch all RELATES_TO edges for a graph."""
        logger.info(f"Fetching all edges for graph {graph_id}...")

        edges: List[Dict[str, Any]] = []
        skip = 0
        cypher = (
            "MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) "
            "WHERE r.group_id = $gid "
            "RETURN r.uuid AS uuid, r.name AS name, r.fact AS fact, "
            "       s.uuid AS source_node_uuid, t.uuid AS target_node_uuid, "
            "       properties(r) AS attributes "
            "SKIP $skip LIMIT $limit"
        )

        driver = self._get_driver()
        while True:
            page_skip = skip
            records = self._call_with_retry(
                lambda s=page_skip: self._run_read(
                    driver, cypher, gid=graph_id, skip=s, limit=_PAGE_SIZE
                ),
                operation_name=f"fetch edges (graph={graph_id}, skip={page_skip})",
            )
            if not records:
                break

            for rec in records:
                attrs = dict(rec["attributes"] or {})
                for k in ("uuid", "name", "fact", "group_id"):
                    attrs.pop(k, None)
                edges.append(
                    {
                        "uuid": rec["uuid"] or "",
                        "name": rec["name"] or "",
                        "fact": rec["fact"] or "",
                        "source_node_uuid": rec["source_node_uuid"] or "",
                        "target_node_uuid": rec["target_node_uuid"] or "",
                        "attributes": attrs,
                    }
                )

            if len(records) < _PAGE_SIZE:
                break
            skip += _PAGE_SIZE

        logger.info(f"Fetched {len(edges)} edges")
        return edges

    def get_node_edges(self, node_uuid: str) -> List[Dict[str, Any]]:
        """Edges touching a single node (in either direction)."""
        cypher = (
            "MATCH (n:Entity {uuid: $uuid})-[r:RELATES_TO]-(other:Entity) "
            "RETURN r.uuid AS uuid, r.name AS name, r.fact AS fact, "
            "       startNode(r).uuid AS source_node_uuid, "
            "       endNode(r).uuid AS target_node_uuid, "
            "       properties(r) AS attributes"
        )
        try:
            driver = self._get_driver()
            records = self._call_with_retry(
                lambda: self._run_read(driver, cypher, uuid=node_uuid),
                operation_name=f"fetch node edges (uuid={node_uuid[:8]})",
            )
            edges: List[Dict[str, Any]] = []
            for rec in records:
                attrs = dict(rec["attributes"] or {})
                for k in ("uuid", "name", "fact", "group_id"):
                    attrs.pop(k, None)
                edges.append(
                    {
                        "uuid": rec["uuid"] or "",
                        "name": rec["name"] or "",
                        "fact": rec["fact"] or "",
                        "source_node_uuid": rec["source_node_uuid"] or "",
                        "target_node_uuid": rec["target_node_uuid"] or "",
                        "attributes": attrs,
                    }
                )
            return edges
        except Exception as e:
            logger.warning(f"Failed fetching edges for node {node_uuid}: {e}")
            return []

    # ------------------------------------------------------------------
    # Higher-level (lifted from zep_entity_reader.py:215-411 — these only
    # consume the dict shape from get_all_nodes/get_all_edges, so they
    # work unchanged once those primitives are correct).
    # ------------------------------------------------------------------

    def filter_defined_entities(
        self,
        graph_id: str,
        defined_entity_types: Optional[List[str]] = None,
        enrich_with_edges: bool = True,
    ) -> FilteredEntities:
        logger.info(f"Filtering defined entities for graph {graph_id}...")

        all_nodes = self.get_all_nodes(graph_id)
        total_count = len(all_nodes)

        all_edges = self.get_all_edges(graph_id) if enrich_with_edges else []
        node_map = {n["uuid"]: n for n in all_nodes}

        filtered_entities: List[EntityNode] = []
        entity_types_found: set = set()

        for node in all_nodes:
            labels = node.get("labels", [])
            custom_labels = [l for l in labels if l not in ["Entity", "Node"]]

            if not custom_labels:
                continue

            if defined_entity_types:
                matching_labels = [l for l in custom_labels if l in defined_entity_types]
                if not matching_labels:
                    continue
                entity_type = matching_labels[0]
            else:
                entity_type = custom_labels[0]

            entity_types_found.add(entity_type)

            entity = EntityNode(
                uuid=node["uuid"],
                name=node["name"],
                labels=labels,
                summary=node["summary"],
                attributes=node["attributes"],
            )

            if enrich_with_edges:
                related_edges: List[Dict[str, Any]] = []
                related_node_uuids: set = set()

                for edge in all_edges:
                    if edge["source_node_uuid"] == node["uuid"]:
                        related_edges.append(
                            {
                                "direction": "outgoing",
                                "edge_name": edge["name"],
                                "fact": edge["fact"],
                                "target_node_uuid": edge["target_node_uuid"],
                            }
                        )
                        related_node_uuids.add(edge["target_node_uuid"])
                    elif edge["target_node_uuid"] == node["uuid"]:
                        related_edges.append(
                            {
                                "direction": "incoming",
                                "edge_name": edge["name"],
                                "fact": edge["fact"],
                                "source_node_uuid": edge["source_node_uuid"],
                            }
                        )
                        related_node_uuids.add(edge["source_node_uuid"])

                entity.related_edges = related_edges

                related_nodes: List[Dict[str, Any]] = []
                for related_uuid in related_node_uuids:
                    if related_uuid in node_map:
                        related_node = node_map[related_uuid]
                        related_nodes.append(
                            {
                                "uuid": related_node["uuid"],
                                "name": related_node["name"],
                                "labels": related_node["labels"],
                                "summary": related_node.get("summary", ""),
                            }
                        )
                entity.related_nodes = related_nodes

            filtered_entities.append(entity)

        logger.info(
            f"Filtered: total={total_count}, matched={len(filtered_entities)}, "
            f"types={entity_types_found}"
        )
        return FilteredEntities(
            entities=filtered_entities,
            entity_types=entity_types_found,
            total_count=total_count,
            filtered_count=len(filtered_entities),
        )

    def get_entity_with_context(
        self, graph_id: str, entity_uuid: str
    ) -> Optional[EntityNode]:
        cypher = (
            "MATCH (n:Entity {uuid: $uuid}) "
            "RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels, "
            "       n.summary AS summary, properties(n) AS attributes "
            "LIMIT 1"
        )
        try:
            driver = self._get_driver()
            records = self._call_with_retry(
                lambda: self._run_read(driver, cypher, uuid=entity_uuid),
                operation_name=f"fetch node detail (uuid={entity_uuid[:8]})",
            )
            if not records:
                return None
            rec = records[0]
            attrs = dict(rec["attributes"] or {})
            for k in ("uuid", "name", "summary", "group_id", "labels", "created_at"):
                attrs.pop(k, None)

            edges = self.get_node_edges(entity_uuid)
            all_nodes = self.get_all_nodes(graph_id)
            node_map = {n["uuid"]: n for n in all_nodes}

            related_edges: List[Dict[str, Any]] = []
            related_node_uuids: set = set()
            for edge in edges:
                if edge["source_node_uuid"] == entity_uuid:
                    related_edges.append(
                        {
                            "direction": "outgoing",
                            "edge_name": edge["name"],
                            "fact": edge["fact"],
                            "target_node_uuid": edge["target_node_uuid"],
                        }
                    )
                    related_node_uuids.add(edge["target_node_uuid"])
                else:
                    related_edges.append(
                        {
                            "direction": "incoming",
                            "edge_name": edge["name"],
                            "fact": edge["fact"],
                            "source_node_uuid": edge["source_node_uuid"],
                        }
                    )
                    related_node_uuids.add(edge["source_node_uuid"])

            related_nodes: List[Dict[str, Any]] = []
            for related_uuid in related_node_uuids:
                if related_uuid in node_map:
                    related_node = node_map[related_uuid]
                    related_nodes.append(
                        {
                            "uuid": related_node["uuid"],
                            "name": related_node["name"],
                            "labels": related_node["labels"],
                            "summary": related_node.get("summary", ""),
                        }
                    )

            return EntityNode(
                uuid=rec["uuid"] or "",
                name=rec["name"] or "",
                labels=list(rec["labels"] or []),
                summary=rec["summary"] or "",
                attributes=attrs,
                related_edges=related_edges,
                related_nodes=related_nodes,
            )
        except Exception as e:
            logger.error(f"Failed fetching entity {entity_uuid}: {e}")
            return None

    def get_entities_by_type(
        self, graph_id: str, entity_type: str, enrich_with_edges: bool = True
    ) -> List[EntityNode]:
        result = self.filter_defined_entities(
            graph_id=graph_id,
            defined_entity_types=[entity_type],
            enrich_with_edges=enrich_with_edges,
        )
        return result.entities

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _run_read(driver, cypher: str, **params) -> List[Dict[str, Any]]:
        with driver.session() as session:
            result = session.run(cypher, **params)
            return [dict(record) for record in result]
