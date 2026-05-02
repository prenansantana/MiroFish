"""
Graphiti+Neo4j tools service.

Mirrors `zep_tools.py:401-1730` (ZepToolsService). The factory in
`_memory_backend.py` returns this when `MEMORY_BACKEND=graphiti`. Public
surface is identical to ZepToolsService so `report_agent.py` and other
call sites work unchanged.

Implementation:
  - Low-level primitives (search_graph, get_all_nodes, get_all_edges,
    get_node_detail, get_node_edges) talk to Graphiti / Neo4j directly.
  - High-level orchestrators (insight_forge, panorama_search, quick_search,
    get_simulation_context, get_entity_summary, get_entities_by_type,
    get_graph_statistics, _local_search) are lifted near-verbatim from
    zep_tools.py — they only consume the primitives + the LLM client.
  - interview_agents and helpers are lifted unchanged because they don't
    touch the memory backend at all (they call SimulationRunner).
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional, TypeVar

from graphiti_core import Graphiti
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, TransientError

from ..config import Config
from ..utils.llm_client import LLMClient
from ..utils.locale import get_locale, t
from ..utils.logger import get_logger
from ._graphiti_clients import make_graphiti
from ._memory_backend import AsyncRunner

# Reuse dataclasses — they describe shapes, not behavior.
from .zep_tools import (  # noqa: F401
    AgentInterview,
    EdgeInfo,
    InsightForgeResult,
    InterviewResult,
    NodeInfo,
    PanoramaResult,
    SearchResult,
)

logger = get_logger('mirofish.graphiti_tools')

T = TypeVar('T')

_PAGE_SIZE = 100
_MAX_NODES = 2000


class GraphitiToolsService:
    """Graphiti equivalent of ZepToolsService. Same public surface."""

    MAX_RETRIES = 3
    RETRY_DELAY = 2.0

    def __init__(
        self,
        api_key: Optional[str] = None,  # ignored; kept for signature compat
        llm_client: Optional[LLMClient] = None,
        neo4j_uri: Optional[str] = None,
        neo4j_user: Optional[str] = None,
        neo4j_password: Optional[str] = None,
    ) -> None:
        self.neo4j_uri = neo4j_uri or Config.NEO4J_URI
        self.neo4j_user = neo4j_user or Config.NEO4J_USER
        self.neo4j_password = neo4j_password or Config.NEO4J_PASSWORD

        if not self.neo4j_password:
            raise ValueError("NEO4J_PASSWORD not configured")

        self._llm_client = llm_client
        self._graphiti: Optional[Graphiti] = None
        self._driver = None  # type: ignore[assignment]
        self._runner: Optional[AsyncRunner] = None
        logger.info("GraphitiToolsService initialized")

    @property
    def llm(self) -> LLMClient:
        if self._llm_client is None:
            self._llm_client = LLMClient()
        return self._llm_client

    def _get_driver(self):
        if self._driver is None:
            self._driver = GraphDatabase.driver(
                self.neo4j_uri, auth=(self.neo4j_user, self.neo4j_password)
            )
        return self._driver

    def _get_runner(self) -> AsyncRunner:
        if self._runner is None:
            self._runner = AsyncRunner(name="GraphitiToolsRunner")
            self._runner.start()
            self._runner.submit(self._init_graphiti(), timeout=60)
        return self._runner

    async def _init_graphiti(self) -> None:
        self._graphiti = make_graphiti(
            self.neo4j_uri, self.neo4j_user, self.neo4j_password
        )
        await self._graphiti.build_indices_and_constraints()

    def close(self) -> None:
        if self._runner is not None and self._graphiti is not None:
            try:
                self._runner.submit(self._graphiti.close(), timeout=10)
            except Exception as e:
                logger.warning(f"Error closing Graphiti: {e}")
        if self._runner is not None:
            self._runner.stop()
            self._runner = None
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def _call_with_retry(
        self,
        func: Callable[[], T],
        operation_name: str,
        max_retries: Optional[int] = None,
    ) -> T:
        max_retries = max_retries or self.MAX_RETRIES
        last_exc: Optional[BaseException] = None
        delay = self.RETRY_DELAY
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

    @staticmethod
    def _run_read(driver, cypher: str, **params) -> List[Dict[str, Any]]:
        with driver.session() as session:
            result = session.run(cypher, **params)
            return [dict(record) for record in result]

    # ==================================================================
    # Search
    # ==================================================================

    def search_graph(
        self,
        graph_id: str,
        query: str,
        limit: int = 10,
        scope: str = "edges",
    ) -> SearchResult:
        """Hybrid (semantic + BM25) search via Graphiti.search, filtered by group_id.

        Falls back to local keyword search on failure (parity with Zep version).
        """
        logger.info(f"Graphiti search graph={graph_id} query={query[:50]}")

        try:
            runner = self._get_runner()
            assert self._graphiti is not None

            edges_results = runner.submit(
                self._graphiti.search(
                    query=query,
                    group_ids=[graph_id],
                    num_results=limit,
                ),
                timeout=60,
            )

            facts: List[str] = []
            edges: List[Dict[str, Any]] = []
            nodes: List[Dict[str, Any]] = []

            for edge in edges_results or []:
                if getattr(edge, 'fact', None):
                    facts.append(edge.fact)
                edges.append(
                    {
                        "uuid": getattr(edge, 'uuid', '') or '',
                        "name": getattr(edge, 'name', '') or '',
                        "fact": getattr(edge, 'fact', '') or '',
                        "source_node_uuid": getattr(edge, 'source_node_uuid', '') or '',
                        "target_node_uuid": getattr(edge, 'target_node_uuid', '') or '',
                    }
                )

            # Optional: also include node hits when scope asks for them.
            if scope in ("nodes", "both"):
                try:
                    from graphiti_core.search.search_config_recipes import (
                        NODE_HYBRID_SEARCH_RRF,
                    )
                    config = NODE_HYBRID_SEARCH_RRF.model_copy(deep=True)
                    config.limit = limit
                    node_results = runner.submit(
                        self._graphiti._search(
                            query=query,
                            config=config,
                            group_ids=[graph_id],
                        ),
                        timeout=60,
                    )
                    for node in getattr(node_results, 'nodes', []) or []:
                        nodes.append(
                            {
                                "uuid": getattr(node, 'uuid', '') or '',
                                "name": getattr(node, 'name', '') or '',
                                "labels": list(getattr(node, 'labels', []) or []),
                                "summary": getattr(node, 'summary', '') or '',
                            }
                        )
                        if getattr(node, 'summary', None):
                            facts.append(f"[{node.name}]: {node.summary}")
                except Exception as e:
                    logger.debug(f"Graphiti node search unavailable: {e}")

            return SearchResult(
                facts=facts, edges=edges, nodes=nodes, query=query, total_count=len(facts)
            )
        except Exception as e:
            logger.warning(f"Graphiti search failed, falling back to local: {e}")
            return self._local_search(graph_id, query, limit, scope)

    def _local_search(
        self, graph_id: str, query: str, limit: int = 10, scope: str = "edges"
    ) -> SearchResult:
        """Keyword-match fallback. Lifted from zep_tools.py:546-648."""
        logger.info(f"Local keyword search query={query[:30]}")

        facts: List[str] = []
        edges_result: List[Dict[str, Any]] = []
        nodes_result: List[Dict[str, Any]] = []

        query_lower = query.lower()
        keywords = [
            w.strip()
            for w in query_lower.replace(',', ' ').replace('，', ' ').split()
            if len(w.strip()) > 1
        ]

        def match_score(text: str) -> int:
            if not text:
                return 0
            tl = text.lower()
            if query_lower in tl:
                return 100
            score = 0
            for kw in keywords:
                if kw in tl:
                    score += 10
            return score

        try:
            if scope in ["edges", "both"]:
                all_edges = self.get_all_edges(graph_id)
                scored = [(match_score(e.fact) + match_score(e.name), e) for e in all_edges]
                scored = [s for s in scored if s[0] > 0]
                scored.sort(key=lambda x: x[0], reverse=True)
                for _, edge in scored[:limit]:
                    if edge.fact:
                        facts.append(edge.fact)
                    edges_result.append(
                        {
                            "uuid": edge.uuid,
                            "name": edge.name,
                            "fact": edge.fact,
                            "source_node_uuid": edge.source_node_uuid,
                            "target_node_uuid": edge.target_node_uuid,
                        }
                    )
            if scope in ["nodes", "both"]:
                all_nodes = self.get_all_nodes(graph_id)
                scored_n = [
                    (match_score(n.name) + match_score(n.summary), n) for n in all_nodes
                ]
                scored_n = [s for s in scored_n if s[0] > 0]
                scored_n.sort(key=lambda x: x[0], reverse=True)
                for _, node in scored_n[:limit]:
                    nodes_result.append(
                        {
                            "uuid": node.uuid,
                            "name": node.name,
                            "labels": node.labels,
                            "summary": node.summary,
                        }
                    )
                    if node.summary:
                        facts.append(f"[{node.name}]: {node.summary}")
        except Exception as e:
            logger.error(f"Local search failed: {e}")

        return SearchResult(
            facts=facts,
            edges=edges_result,
            nodes=nodes_result,
            query=query,
            total_count=len(facts),
        )

    # ==================================================================
    # Node/edge primitives (Cypher-backed)
    # ==================================================================

    def get_all_nodes(self, graph_id: str) -> List[NodeInfo]:
        logger.info(f"Fetching all nodes for graph {graph_id}")

        result: List[NodeInfo] = []
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
                for k in ("uuid", "name", "summary", "group_id", "labels", "created_at"):
                    attrs.pop(k, None)
                result.append(
                    NodeInfo(
                        uuid=rec["uuid"] or "",
                        name=rec["name"] or "",
                        labels=list(rec["labels"] or []),
                        summary=rec["summary"] or "",
                        attributes=attrs,
                    )
                )
            if len(result) >= _MAX_NODES:
                logger.warning(
                    f"Node count reached limit ({_MAX_NODES}) for graph {graph_id}"
                )
                result = result[:_MAX_NODES]
                break
            if len(records) < _PAGE_SIZE:
                break
            skip += _PAGE_SIZE

        logger.info(f"Fetched {len(result)} nodes")
        return result

    def get_all_edges(
        self, graph_id: str, include_temporal: bool = True
    ) -> List[EdgeInfo]:
        logger.info(f"Fetching all edges for graph {graph_id}")

        result: List[EdgeInfo] = []
        skip = 0
        cypher = (
            "MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) "
            "WHERE r.group_id = $gid "
            "RETURN r.uuid AS uuid, r.name AS name, r.fact AS fact, "
            "       s.uuid AS source_node_uuid, t.uuid AS target_node_uuid, "
            "       r.created_at AS created_at, r.valid_at AS valid_at, "
            "       r.invalid_at AS invalid_at, r.expired_at AS expired_at "
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
                edge = EdgeInfo(
                    uuid=rec["uuid"] or "",
                    name=rec["name"] or "",
                    fact=rec["fact"] or "",
                    source_node_uuid=rec["source_node_uuid"] or "",
                    target_node_uuid=rec["target_node_uuid"] or "",
                )
                if include_temporal:
                    edge.created_at = self._iso(rec.get("created_at"))
                    edge.valid_at = self._iso(rec.get("valid_at"))
                    edge.invalid_at = self._iso(rec.get("invalid_at"))
                    edge.expired_at = self._iso(rec.get("expired_at"))
                result.append(edge)
            if len(records) < _PAGE_SIZE:
                break
            skip += _PAGE_SIZE

        logger.info(f"Fetched {len(result)} edges")
        return result

    def get_node_detail(self, node_uuid: str) -> Optional[NodeInfo]:
        cypher = (
            "MATCH (n:Entity {uuid: $uuid}) "
            "RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels, "
            "       n.summary AS summary, properties(n) AS attributes "
            "LIMIT 1"
        )
        try:
            driver = self._get_driver()
            records = self._call_with_retry(
                lambda: self._run_read(driver, cypher, uuid=node_uuid),
                operation_name=f"fetch node detail (uuid={node_uuid[:8]})",
            )
            if not records:
                return None
            rec = records[0]
            attrs = dict(rec["attributes"] or {})
            for k in ("uuid", "name", "summary", "group_id", "labels", "created_at"):
                attrs.pop(k, None)
            return NodeInfo(
                uuid=rec["uuid"] or "",
                name=rec["name"] or "",
                labels=list(rec["labels"] or []),
                summary=rec["summary"] or "",
                attributes=attrs,
            )
        except Exception as e:
            logger.error(f"Failed fetching node {node_uuid}: {e}")
            return None

    def get_node_edges(self, graph_id: str, node_uuid: str) -> List[EdgeInfo]:
        cypher = (
            "MATCH (n:Entity {uuid: $uuid})-[r:RELATES_TO]-(other:Entity) "
            "WHERE r.group_id = $gid "
            "RETURN r.uuid AS uuid, r.name AS name, r.fact AS fact, "
            "       startNode(r).uuid AS source_node_uuid, "
            "       endNode(r).uuid AS target_node_uuid, "
            "       r.created_at AS created_at, r.valid_at AS valid_at, "
            "       r.invalid_at AS invalid_at, r.expired_at AS expired_at"
        )
        try:
            driver = self._get_driver()
            records = self._call_with_retry(
                lambda: self._run_read(driver, cypher, uuid=node_uuid, gid=graph_id),
                operation_name=f"fetch node edges (uuid={node_uuid[:8]})",
            )
            edges: List[EdgeInfo] = []
            for rec in records:
                edge = EdgeInfo(
                    uuid=rec["uuid"] or "",
                    name=rec["name"] or "",
                    fact=rec["fact"] or "",
                    source_node_uuid=rec["source_node_uuid"] or "",
                    target_node_uuid=rec["target_node_uuid"] or "",
                )
                edge.created_at = self._iso(rec.get("created_at"))
                edge.valid_at = self._iso(rec.get("valid_at"))
                edge.invalid_at = self._iso(rec.get("invalid_at"))
                edge.expired_at = self._iso(rec.get("expired_at"))
                edges.append(edge)
            return edges
        except Exception as e:
            logger.warning(f"Failed fetching edges for node {node_uuid}: {e}")
            return []

    @staticmethod
    def _iso(value: Any) -> Optional[str]:
        if value is None:
            return None
        # neo4j returns DateTime objects; isoformat() works on them.
        if hasattr(value, 'isoformat'):
            return value.isoformat()
        return str(value)

    # ==================================================================
    # Higher-level (lifted from zep_tools.py)
    # ==================================================================

    def get_entities_by_type(
        self, graph_id: str, entity_type: str
    ) -> List[NodeInfo]:
        logger.info(f"Fetching entities by type: {entity_type}")
        all_nodes = self.get_all_nodes(graph_id)
        filtered = [n for n in all_nodes if entity_type in n.labels]
        logger.info(f"Found {len(filtered)} entities of type {entity_type}")
        return filtered

    def get_entity_summary(
        self, graph_id: str, entity_name: str
    ) -> Dict[str, Any]:
        logger.info(f"Fetching entity summary: {entity_name}")
        search_result = self.search_graph(graph_id=graph_id, query=entity_name, limit=20)
        all_nodes = self.get_all_nodes(graph_id)
        entity_node = None
        for node in all_nodes:
            if node.name.lower() == entity_name.lower():
                entity_node = node
                break
        related_edges: List[EdgeInfo] = []
        if entity_node:
            related_edges = self.get_node_edges(graph_id, entity_node.uuid)
        return {
            "entity_name": entity_name,
            "entity_info": entity_node.to_dict() if entity_node else None,
            "related_facts": search_result.facts,
            "related_edges": [e.to_dict() for e in related_edges],
            "total_relations": len(related_edges),
        }

    def get_graph_statistics(self, graph_id: str) -> Dict[str, Any]:
        logger.info(f"Fetching graph statistics for {graph_id}")
        nodes = self.get_all_nodes(graph_id)
        edges = self.get_all_edges(graph_id)

        entity_types: Dict[str, int] = {}
        for node in nodes:
            for label in node.labels:
                if label not in ["Entity", "Node"]:
                    entity_types[label] = entity_types.get(label, 0) + 1

        relation_types: Dict[str, int] = {}
        for edge in edges:
            relation_types[edge.name] = relation_types.get(edge.name, 0) + 1

        return {
            "graph_id": graph_id,
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "entity_types": entity_types,
            "relation_types": relation_types,
        }

    def get_simulation_context(
        self, graph_id: str, simulation_requirement: str, limit: int = 30
    ) -> Dict[str, Any]:
        logger.info(f"Fetching simulation context: {simulation_requirement[:50]}")
        search_result = self.search_graph(
            graph_id=graph_id, query=simulation_requirement, limit=limit
        )
        stats = self.get_graph_statistics(graph_id)
        all_nodes = self.get_all_nodes(graph_id)

        entities = []
        for node in all_nodes:
            custom_labels = [l for l in node.labels if l not in ["Entity", "Node"]]
            if custom_labels:
                entities.append(
                    {
                        "name": node.name,
                        "type": custom_labels[0],
                        "summary": node.summary,
                    }
                )
        return {
            "simulation_requirement": simulation_requirement,
            "related_facts": search_result.facts,
            "graph_statistics": stats,
            "entities": entities[:limit],
            "total_entities": len(entities),
        }

    # ------------------------------------------------------------------
    # InsightForge — lifted near-verbatim from zep_tools.py:945-1090
    # ------------------------------------------------------------------

    def insight_forge(
        self,
        graph_id: str,
        query: str,
        simulation_requirement: str,
        report_context: str = "",
        max_sub_queries: int = 5,
    ) -> InsightForgeResult:
        logger.info(f"InsightForge start: {query[:50]}")
        result = InsightForgeResult(
            query=query,
            simulation_requirement=simulation_requirement,
            sub_queries=[],
        )

        sub_queries = self._generate_sub_queries(
            query=query,
            simulation_requirement=simulation_requirement,
            report_context=report_context,
            max_queries=max_sub_queries,
        )
        result.sub_queries = sub_queries
        logger.info(f"Generated {len(sub_queries)} sub-queries")

        all_facts: List[str] = []
        all_edges: List[Dict[str, Any]] = []
        seen_facts: set = set()

        for sub_query in sub_queries:
            search_result = self.search_graph(
                graph_id=graph_id, query=sub_query, limit=15, scope="edges"
            )
            for fact in search_result.facts:
                if fact not in seen_facts:
                    all_facts.append(fact)
                    seen_facts.add(fact)
            all_edges.extend(search_result.edges)

        main_search = self.search_graph(
            graph_id=graph_id, query=query, limit=20, scope="edges"
        )
        for fact in main_search.facts:
            if fact not in seen_facts:
                all_facts.append(fact)
                seen_facts.add(fact)

        result.semantic_facts = all_facts
        result.total_facts = len(all_facts)

        entity_uuids: set = set()
        for edge_data in all_edges:
            if isinstance(edge_data, dict):
                source_uuid = edge_data.get('source_node_uuid', '')
                target_uuid = edge_data.get('target_node_uuid', '')
                if source_uuid:
                    entity_uuids.add(source_uuid)
                if target_uuid:
                    entity_uuids.add(target_uuid)

        entity_insights: List[Dict[str, Any]] = []
        node_map: Dict[str, NodeInfo] = {}

        for uuid in list(entity_uuids):
            if not uuid:
                continue
            try:
                node = self.get_node_detail(uuid)
                if node:
                    node_map[uuid] = node
                    entity_type = next(
                        (l for l in node.labels if l not in ["Entity", "Node"]), "实体"
                    )
                    related_facts = [
                        f for f in all_facts if node.name.lower() in f.lower()
                    ]
                    entity_insights.append(
                        {
                            "uuid": node.uuid,
                            "name": node.name,
                            "type": entity_type,
                            "summary": node.summary,
                            "related_facts": related_facts,
                        }
                    )
            except Exception as e:
                logger.debug(f"Failed fetching node {uuid}: {e}")
                continue

        result.entity_insights = entity_insights
        result.total_entities = len(entity_insights)

        relationship_chains: List[str] = []
        for edge_data in all_edges:
            if isinstance(edge_data, dict):
                source_uuid = edge_data.get('source_node_uuid', '')
                target_uuid = edge_data.get('target_node_uuid', '')
                relation_name = edge_data.get('name', '')
                source_name = (
                    node_map.get(source_uuid, NodeInfo('', '', [], '', {})).name
                    or source_uuid[:8]
                )
                target_name = (
                    node_map.get(target_uuid, NodeInfo('', '', [], '', {})).name
                    or target_uuid[:8]
                )
                chain = f"{source_name} --[{relation_name}]--> {target_name}"
                if chain not in relationship_chains:
                    relationship_chains.append(chain)

        result.relationship_chains = relationship_chains
        result.total_relationships = len(relationship_chains)

        logger.info(
            f"InsightForge complete: facts={result.total_facts}, "
            f"entities={result.total_entities}, "
            f"relationships={result.total_relationships}"
        )
        return result

    def _generate_sub_queries(
        self,
        query: str,
        simulation_requirement: str,
        report_context: str = "",
        max_queries: int = 5,
    ) -> List[str]:
        """Lifted verbatim from zep_tools.py:1092-1143."""
        system_prompt = """你是一个专业的问题分析专家。你的任务是将一个复杂问题分解为多个可以在模拟世界中独立观察的子问题。

要求：
1. 每个子问题应该足够具体，可以在模拟世界中找到相关的Agent行为或事件
2. 子问题应该覆盖原问题的不同维度（如：谁、什么、为什么、怎么样、何时、何地）
3. 子问题应该与模拟场景相关
4. 返回JSON格式：{"sub_queries": ["子问题1", "子问题2", ...]}"""

        user_prompt = f"""模拟需求背景：
{simulation_requirement}

{f"报告上下文：{report_context[:500]}" if report_context else ""}

请将以下问题分解为{max_queries}个子问题：
{query}

返回JSON格式的子问题列表。"""

        try:
            response = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
            )
            sub_queries = response.get("sub_queries", [])
            return [str(sq) for sq in sub_queries[:max_queries]]
        except Exception as e:
            logger.warning(f"Sub-query generation failed: {e}")
            return [
                query,
                f"{query} 的主要参与者",
                f"{query} 的原因和影响",
                f"{query} 的发展过程",
            ][:max_queries]

    def panorama_search(
        self,
        graph_id: str,
        query: str,
        include_expired: bool = True,
        limit: int = 50,
    ) -> PanoramaResult:
        """Lifted from zep_tools.py:1145-1235."""
        logger.info(f"Panorama search start: {query[:50]}")
        result = PanoramaResult(query=query)

        all_nodes = self.get_all_nodes(graph_id)
        node_map = {n.uuid: n for n in all_nodes}
        result.all_nodes = all_nodes
        result.total_nodes = len(all_nodes)

        all_edges = self.get_all_edges(graph_id, include_temporal=True)
        result.all_edges = all_edges
        result.total_edges = len(all_edges)

        active_facts: List[str] = []
        historical_facts: List[str] = []

        for edge in all_edges:
            if not edge.fact:
                continue
            is_historical = edge.is_expired or edge.is_invalid
            if is_historical:
                valid_at = edge.valid_at or "未知"
                invalid_at = edge.invalid_at or edge.expired_at or "未知"
                fact_with_time = f"[{valid_at} - {invalid_at}] {edge.fact}"
                historical_facts.append(fact_with_time)
            else:
                active_facts.append(edge.fact)

        query_lower = query.lower()
        keywords = [
            w.strip()
            for w in query_lower.replace(',', ' ').replace('，', ' ').split()
            if len(w.strip()) > 1
        ]

        def relevance_score(fact: str) -> int:
            fl = fact.lower()
            score = 0
            if query_lower in fl:
                score += 100
            for kw in keywords:
                if kw in fl:
                    score += 10
            return score

        active_facts.sort(key=relevance_score, reverse=True)
        historical_facts.sort(key=relevance_score, reverse=True)

        result.active_facts = active_facts[:limit]
        result.historical_facts = historical_facts[:limit] if include_expired else []
        result.active_count = len(active_facts)
        result.historical_count = len(historical_facts)

        logger.info(
            f"Panorama complete: active={result.active_count}, "
            f"historical={result.historical_count}"
        )
        return result

    def quick_search(
        self, graph_id: str, query: str, limit: int = 10
    ) -> SearchResult:
        logger.info(f"Quick search: {query[:50]}")
        result = self.search_graph(
            graph_id=graph_id, query=query, limit=limit, scope="edges"
        )
        logger.info(f"Quick search complete: {result.total_count}")
        return result

    # ------------------------------------------------------------------
    # interview_agents and helpers — lifted unchanged from zep_tools.py.
    # They don't touch the memory backend; they call SimulationRunner.
    # ------------------------------------------------------------------

    def interview_agents(
        self,
        simulation_id: str,
        interview_requirement: str,
        simulation_requirement: str = "",
        max_agents: int = 5,
        custom_questions: Optional[List[str]] = None,
    ) -> InterviewResult:
        from .simulation_runner import SimulationRunner

        logger.info(f"Interview agents start: {interview_requirement[:50]}")
        result = InterviewResult(
            interview_topic=interview_requirement,
            interview_questions=custom_questions or [],
        )

        profiles = self._load_agent_profiles(simulation_id)
        if not profiles:
            logger.warning(f"Profiles not found for simulation {simulation_id}")
            result.summary = "未找到可采访的Agent人设文件"
            return result

        result.total_agents = len(profiles)
        logger.info(f"Loaded {len(profiles)} profiles")

        selected_agents, selected_indices, selection_reasoning = (
            self._select_agents_for_interview(
                profiles=profiles,
                interview_requirement=interview_requirement,
                simulation_requirement=simulation_requirement,
                max_agents=max_agents,
            )
        )

        result.selected_agents = selected_agents
        result.selection_reasoning = selection_reasoning
        logger.info(
            f"Selected {len(selected_agents)} agents for interview: "
            f"indices={selected_indices}"
        )

        if not result.interview_questions:
            result.interview_questions = self._generate_interview_questions(
                interview_requirement=interview_requirement,
                simulation_requirement=simulation_requirement,
                selected_agents=selected_agents,
            )
            logger.info(
                f"Generated {len(result.interview_questions)} interview questions"
            )

        combined_prompt = "\n".join(
            [f"{i+1}. {q}" for i, q in enumerate(result.interview_questions)]
        )

        INTERVIEW_PROMPT_PREFIX = (
            "你正在接受一次采访。请结合你的人设、所有的过往记忆与行动，"
            "以纯文本方式直接回答以下问题。\n"
            "回复要求：\n"
            "1. 直接用自然语言回答，不要调用任何工具\n"
            "2. 不要返回JSON格式或工具调用格式\n"
            "3. 不要使用Markdown标题（如#、##、###）\n"
            "4. 按问题编号逐一回答，每个回答以「问题X：」开头（X为问题编号）\n"
            "5. 每个问题的回答之间用空行分隔\n"
            "6. 回答要有实质内容，每个问题至少回答2-3句话\n\n"
        )
        optimized_prompt = f"{INTERVIEW_PROMPT_PREFIX}{combined_prompt}"

        try:
            interviews_request = []
            for agent_idx in selected_indices:
                interviews_request.append(
                    {"agent_id": agent_idx, "prompt": optimized_prompt}
                )

            logger.info(f"Calling batch interview API: count={len(interviews_request)}")

            api_result = SimulationRunner.interview_agents_batch(
                simulation_id=simulation_id,
                interviews=interviews_request,
                platform=None,
                timeout=180.0,
            )

            logger.info(
                f"Interview API returned: count={api_result.get('interviews_count', 0)} "
                f"success={api_result.get('success')}"
            )

            if not api_result.get("success", False):
                error_msg = api_result.get("error", "未知错误")
                logger.warning(f"Interview API returned failure: {error_msg}")
                result.summary = (
                    f"采访API调用失败：{error_msg}。请检查OASIS模拟环境状态。"
                )
                return result

            api_data = api_result.get("result", {})
            results_dict = (
                api_data.get("results", {}) if isinstance(api_data, dict) else {}
            )

            for i, agent_idx in enumerate(selected_indices):
                agent = selected_agents[i]
                agent_name = agent.get(
                    "realname", agent.get("username", f"Agent_{agent_idx}")
                )
                agent_role = agent.get("profession", "未知")
                agent_bio = agent.get("bio", "")

                twitter_result = results_dict.get(f"twitter_{agent_idx}", {})
                reddit_result = results_dict.get(f"reddit_{agent_idx}", {})

                twitter_response = twitter_result.get("response", "")
                reddit_response = reddit_result.get("response", "")

                twitter_response = self._clean_tool_call_response(twitter_response)
                reddit_response = self._clean_tool_call_response(reddit_response)

                twitter_text = twitter_response if twitter_response else "（该平台未获得回复）"
                reddit_text = reddit_response if reddit_response else "（该平台未获得回复）"
                response_text = (
                    f"【Twitter平台回答】\n{twitter_text}\n\n"
                    f"【Reddit平台回答】\n{reddit_text}"
                )

                import re
                combined_responses = f"{twitter_response} {reddit_response}"

                clean_text = re.sub(r'#{1,6}\s+', '', combined_responses)
                clean_text = re.sub(r'\{[^}]*tool_name[^}]*\}', '', clean_text)
                clean_text = re.sub(r'[*_`|>~\-]{2,}', '', clean_text)
                clean_text = re.sub(r'问题\d+[：:]\s*', '', clean_text)
                clean_text = re.sub(r'【[^】]+】', '', clean_text)

                sentences = re.split(r'[。！？]', clean_text)
                meaningful = [
                    s.strip()
                    for s in sentences
                    if 20 <= len(s.strip()) <= 150
                    and not re.match(r'^[\s\W，,；;：:、]+', s.strip())
                    and not s.strip().startswith(('{', '问题'))
                ]
                meaningful.sort(key=len, reverse=True)
                key_quotes = [s + "。" for s in meaningful[:3]]

                if not key_quotes:
                    paired = re.findall(r'“([^“”]{15,100})”', clean_text)
                    paired += re.findall(r'「([^「」]{15,100})」', clean_text)
                    key_quotes = [q for q in paired if not re.match(r'^[，,；;：:、]', q)][:3]

                interview = AgentInterview(
                    agent_name=agent_name,
                    agent_role=agent_role,
                    agent_bio=agent_bio[:1000],
                    question=combined_prompt,
                    response=response_text,
                    key_quotes=key_quotes[:5],
                )
                result.interviews.append(interview)

            result.interviewed_count = len(result.interviews)

        except ValueError as e:
            logger.warning(f"Interview API call failed: {e}")
            result.summary = (
                f"采访失败：{str(e)}。模拟环境可能已关闭，请确保OASIS环境正在运行。"
            )
            return result
        except Exception as e:
            logger.error(f"Interview API call exception: {e}")
            import traceback
            logger.error(traceback.format_exc())
            result.summary = f"采访过程发生错误：{str(e)}"
            return result

        if result.interviews:
            result.summary = self._generate_interview_summary(
                interviews=result.interviews,
                interview_requirement=interview_requirement,
            )

        logger.info(f"Interview agents complete: count={result.interviewed_count}")
        return result

    @staticmethod
    def _clean_tool_call_response(response: str) -> str:
        if not response or not response.strip().startswith('{'):
            return response
        text = response.strip()
        if 'tool_name' not in text[:80]:
            return response
        import re as _re
        try:
            data = json.loads(text)
            if isinstance(data, dict) and 'arguments' in data:
                for key in ('content', 'text', 'body', 'message', 'reply'):
                    if key in data['arguments']:
                        return str(data['arguments'][key])
        except (json.JSONDecodeError, KeyError, TypeError):
            match = _re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
            if match:
                return match.group(1).replace('\\n', '\n').replace('\\"', '"')
        return response

    def _load_agent_profiles(self, simulation_id: str) -> List[Dict[str, Any]]:
        import csv
        import os

        sim_dir = os.path.join(
            os.path.dirname(__file__), f'../../uploads/simulations/{simulation_id}'
        )

        profiles: List[Dict[str, Any]] = []

        reddit_profile_path = os.path.join(sim_dir, "reddit_profiles.json")
        if os.path.exists(reddit_profile_path):
            try:
                with open(reddit_profile_path, 'r', encoding='utf-8') as f:
                    profiles = json.load(f)
                logger.info(f"Loaded {len(profiles)} Reddit profiles")
                return profiles
            except Exception as e:
                logger.warning(f"Failed reading Reddit profiles: {e}")

        twitter_profile_path = os.path.join(sim_dir, "twitter_profiles.csv")
        if os.path.exists(twitter_profile_path):
            try:
                with open(twitter_profile_path, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        profiles.append(
                            {
                                "realname": row.get("name", ""),
                                "username": row.get("username", ""),
                                "bio": row.get("description", ""),
                                "persona": row.get("user_char", ""),
                                "profession": "未知",
                            }
                        )
                logger.info(f"Loaded {len(profiles)} Twitter profiles")
                return profiles
            except Exception as e:
                logger.warning(f"Failed reading Twitter profiles: {e}")

        return profiles

    def _select_agents_for_interview(
        self,
        profiles: List[Dict[str, Any]],
        interview_requirement: str,
        simulation_requirement: str,
        max_agents: int,
    ) -> tuple:
        agent_summaries = []
        for i, profile in enumerate(profiles):
            agent_summaries.append(
                {
                    "index": i,
                    "name": profile.get(
                        "realname", profile.get("username", f"Agent_{i}")
                    ),
                    "profession": profile.get("profession", "未知"),
                    "bio": profile.get("bio", "")[:200],
                    "interested_topics": profile.get("interested_topics", []),
                }
            )

        system_prompt = """你是一个专业的采访策划专家。你的任务是根据采访需求，从模拟Agent列表中选择最适合采访的对象。

选择标准：
1. Agent的身份/职业与采访主题相关
2. Agent可能持有独特或有价值的观点
3. 选择多样化的视角（如：支持方、反对方、中立方、专业人士等）
4. 优先选择与事件直接相关的角色

返回JSON格式：
{
    "selected_indices": [选中Agent的索引列表],
    "reasoning": "选择理由说明"
}"""

        user_prompt = f"""采访需求：
{interview_requirement}

模拟背景：
{simulation_requirement if simulation_requirement else "未提供"}

可选择的Agent列表（共{len(agent_summaries)}个）：
{json.dumps(agent_summaries, ensure_ascii=False, indent=2)}

请选择最多{max_agents}个最适合采访的Agent，并说明选择理由。"""

        try:
            response = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
            )
            selected_indices = response.get("selected_indices", [])[:max_agents]
            reasoning = response.get("reasoning", "基于相关性自动选择")

            selected_agents: List[Dict[str, Any]] = []
            valid_indices: List[int] = []
            for idx in selected_indices:
                if 0 <= idx < len(profiles):
                    selected_agents.append(profiles[idx])
                    valid_indices.append(idx)
            return selected_agents, valid_indices, reasoning
        except Exception as e:
            logger.warning(f"LLM agent selection failed: {e}")
            selected = profiles[:max_agents]
            indices = list(range(min(max_agents, len(profiles))))
            return selected, indices, "使用默认选择策略"

    def _generate_interview_questions(
        self,
        interview_requirement: str,
        simulation_requirement: str,
        selected_agents: List[Dict[str, Any]],
    ) -> List[str]:
        agent_roles = [a.get("profession", "未知") for a in selected_agents]

        system_prompt = """你是一个专业的记者/采访者。根据采访需求，生成3-5个深度采访问题。

问题要求：
1. 开放性问题，鼓励详细回答
2. 针对不同角色可能有不同答案
3. 涵盖事实、观点、感受等多个维度
4. 语言自然，像真实采访一样
5. 每个问题控制在50字以内，简洁明了
6. 直接提问，不要包含背景说明或前缀

返回JSON格式：{"questions": ["问题1", "问题2", ...]}"""

        user_prompt = f"""采访需求：{interview_requirement}

模拟背景：{simulation_requirement if simulation_requirement else "未提供"}

采访对象角色：{', '.join(agent_roles)}

请生成3-5个采访问题。"""

        try:
            response = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.5,
            )
            return response.get(
                "questions", [f"关于{interview_requirement}，您有什么看法？"]
            )
        except Exception as e:
            logger.warning(f"Interview question generation failed: {e}")
            return [
                f"关于{interview_requirement}，您的观点是什么？",
                "这件事对您或您所代表的群体有什么影响？",
                "您认为应该如何解决或改进这个问题？",
            ]

    def _generate_interview_summary(
        self,
        interviews: List[AgentInterview],
        interview_requirement: str,
    ) -> str:
        if not interviews:
            return "未完成任何采访"

        interview_texts = []
        for interview in interviews:
            interview_texts.append(
                f"【{interview.agent_name}（{interview.agent_role}）】\n"
                f"{interview.response[:500]}"
            )

        quote_instruction = (
            "引用受访者原话时使用中文引号「」"
            if get_locale() == 'zh'
            else 'Use quotation marks "" when quoting interviewees'
        )
        system_prompt = f"""你是一个专业的新闻编辑。请根据多位受访者的回答，生成一份采访摘要。

摘要要求：
1. 提炼各方主要观点
2. 指出观点的共识和分歧
3. 突出有价值的引言
4. 客观中立，不偏袒任何一方
5. 控制在1000字内

格式约束（必须遵守）：
- 使用纯文本段落，用空行分隔不同部分
- 不要使用Markdown标题（如#、##、###）
- 不要使用分割线（如---、***）
- {quote_instruction}
- 可以使用**加粗**标记关键词，但不要使用其他Markdown语法"""

        user_prompt = f"""采访主题：{interview_requirement}

采访内容：
{"".join(interview_texts)}

请生成采访摘要。"""

        try:
            response = self.llm.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.5,
            )
            return response or "（无摘要）"
        except Exception as e:
            logger.warning(f"Interview summary generation failed: {e}")
            return "（摘要生成失败）"
