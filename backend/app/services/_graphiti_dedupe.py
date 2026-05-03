"""
In-process dedupe for Graphiti+Neo4j knowledge graphs.

Graphiti's `add_episode` resolves duplicates by embedding similarity, but
only against entities already committed at the moment the call resolves.
When chunks are processed in parallel, two simultaneous chunks can each
create their own node for the same real-world entity before either has
committed.

`GraphitiGraphBuilder` mitigates this with a sequential warm-up phase
(first N chunks run one at a time so core entities are committed before
any parallel batch tries to dedupe). What slips through despite the
warm-up is caught here, automatically, at the end of the build.

Two phases:

  Phase 1 (always safe, applied automatically):
    Exact-name duplicates inside the same group_id with the same custom
    label class. These are guaranteed to be the same entity — their
    relationships are merged into the most-connected node.

  Phase 2 (alias candidates, NOT applied automatically):
    Short-form vs long-form name pairs (e.g. "Smith" vs "Dr. Jane
    Smith", or "Springfield" vs "North Springfield") — shared tokens
    can be coincidence (different person, parent vs child region).
    Returned for review only; never auto-merged.

The CLI in `backend/scripts/dedupe_graph.py` is a thin wrapper around
this module for ad-hoc runs against existing graphs.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from ..utils.logger import get_logger

logger = get_logger('mirofish.graphiti_dedupe')


def _normalize(name: str) -> str:
    return " ".join((name or "").lower().strip().split())


def _custom_labels(labels: List[str]) -> List[str]:
    return [l for l in (labels or []) if l != "Entity"]


def find_exact_duplicates(driver, graph_id: str) -> List[Tuple[Dict, List[Dict]]]:
    """Return (canonical, duplicates) groups for nodes sharing
    normalised name + custom label class within the given graph.

    Canonical = the node with the most relationships (oldest if tied)
    so we keep the most-connected uuid.
    """
    cypher = (
        "MATCH (n:Entity) WHERE n.group_id = $gid "
        "OPTIONAL MATCH (n)-[r:RELATES_TO]-() "
        "RETURN n.uuid AS uuid, n.name AS name, "
        "       [l IN labels(n) WHERE l <> 'Entity'] AS labels, "
        "       n.created_at AS created_at, "
        "       count(r) AS rel_count"
    )
    with driver.session() as s:
        rows = [dict(r) for r in s.run(cypher, gid=graph_id)]

    buckets: Dict[Tuple[str, str], List[Dict]] = {}
    for r in rows:
        labels = _custom_labels(r["labels"])
        key = (_normalize(r["name"]), "/".join(sorted(labels)))
        if not key[0]:
            continue
        buckets.setdefault(key, []).append(r)

    groups: List[Tuple[Dict, List[Dict]]] = []
    for items in buckets.values():
        if len(items) <= 1:
            continue
        items.sort(
            key=lambda x: (-x["rel_count"], str(x["created_at"] or ""))
        )
        groups.append((items[0], items[1:]))
    return groups


def find_alias_candidates(driver, graph_id: str) -> List[Tuple[Dict, Dict]]:
    """Return (long_form, short_form) candidates for review only.

    Conservative heuristic: short_form's tokens are a strict prefix or
    strict suffix of long_form's tokens, and both share at least one
    custom label. NEVER applied automatically because shared tokens can
    be coincidence — a single shared surname does not imply two named
    persons are the same individual.
    """
    cypher = (
        "MATCH (n:Entity) WHERE n.group_id = $gid "
        "RETURN n.uuid AS uuid, n.name AS name, "
        "       [l IN labels(n) WHERE l <> 'Entity'] AS labels"
    )
    with driver.session() as s:
        rows = [dict(r) for r in s.run(cypher, gid=graph_id)]

    rows.sort(key=lambda r: -len(r["name"] or ""))
    seen: set = set()
    candidates: List[Tuple[Dict, Dict]] = []
    for i, long_node in enumerate(rows):
        if long_node["uuid"] in seen:
            continue
        long_norm = _normalize(long_node["name"])
        long_tok = long_norm.split()
        if len(long_tok) < 2:
            continue
        for j in range(i + 1, len(rows)):
            short_node = rows[j]
            if short_node["uuid"] in seen:
                continue
            short_norm = _normalize(short_node["name"])
            if not short_norm or short_norm == long_norm:
                continue
            short_tok = short_norm.split()
            l_types = set(_custom_labels(long_node["labels"]))
            s_types = set(_custom_labels(short_node["labels"]))
            if l_types and s_types and not (l_types & s_types):
                continue
            is_prefix = long_tok[: len(short_tok)] == short_tok
            is_suffix = long_tok[-len(short_tok):] == short_tok
            if not (is_prefix or is_suffix):
                continue
            candidates.append((long_node, short_node))
            seen.add(short_node["uuid"])
    return candidates


def merge_into_canonical(driver, canonical_uuid: str, dupe_uuid: str) -> None:
    """Move all relationships from `dupe_uuid` to `canonical_uuid`,
    then delete the duplicate. Uses APOC if available, falls back to
    pure Cypher otherwise.
    """
    with driver.session() as s:
        # Try APOC first — single call, preserves attributes properly.
        try:
            s.run(
                "MATCH (c:Entity {uuid: $c}), (d:Entity {uuid: $d}) "
                "CALL apoc.refactor.mergeNodes([c, d], "
                "  {properties: 'discard', mergeRels: true}) "
                "YIELD node RETURN node.uuid",
                c=canonical_uuid, d=dupe_uuid,
            ).single()
            return
        except Exception:
            pass
        # Fallback: 3-pass pure Cypher.
        s.run(
            "MATCH (d:Entity {uuid: $d})-[r:RELATES_TO]->(t) "
            "MATCH (c:Entity {uuid: $c}) WHERE c <> t "
            "CREATE (c)-[r2:RELATES_TO]->(t) SET r2 = properties(r) DELETE r",
            c=canonical_uuid, d=dupe_uuid,
        )
        s.run(
            "MATCH (s)-[r:RELATES_TO]->(d:Entity {uuid: $d}) "
            "MATCH (c:Entity {uuid: $c}) WHERE s <> c "
            "CREATE (s)-[r2:RELATES_TO]->(c) SET r2 = properties(r) DELETE r",
            c=canonical_uuid, d=dupe_uuid,
        )
        s.run("MATCH (d:Entity {uuid: $d}) DETACH DELETE d", d=dupe_uuid)


def auto_merge_exact_duplicates(driver, graph_id: str) -> Dict[str, int]:
    """Run phase 1 dedupe end-to-end. Safe for automatic invocation.

    Returns counts so callers can log and surface the result.
    """
    groups = find_exact_duplicates(driver, graph_id)
    total_dupes = sum(len(d) for _, d in groups)
    if not groups:
        return {"groups": 0, "merges": 0}

    logger.info(
        f"Auto-dedup ({graph_id}): {len(groups)} group(s), "
        f"{total_dupes} duplicate(s) to merge"
    )
    for canonical, dupes in groups:
        labels = "/".join(_custom_labels(canonical["labels"]) or ["Entity"])
        for d in dupes:
            logger.info(
                f"  ({labels}) {canonical['name']!r} <- merging duplicate "
                f"{d['uuid'][:8]}..."
            )
            merge_into_canonical(driver, canonical["uuid"], d["uuid"])
    logger.info(
        f"Auto-dedup ({graph_id}) done: {total_dupes} duplicate node(s) merged"
    )
    return {"groups": len(groups), "merges": total_dupes}
