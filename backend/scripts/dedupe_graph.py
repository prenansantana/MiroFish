#!/usr/bin/env python3
"""
Post-build deduplication for Graphiti+Neo4j knowledge graphs.

Why this exists: Graphiti's `add_episode` deduplicates entities by
embedding similarity, but only against entities already committed to the
graph at the moment the call resolves. When `GraphitiGraphBuilder` fans
chunks out in parallel, two simultaneous chunks can each create a node
for the same real-world entity (one with the short form of a name, the
other with the long form) before either has committed.

The builder mitigates this with a sequential warm-up phase, but rare
collisions still slip through. This script catches the rest.

Two phases — both conservative by default:

  Phase 1 (always safe): exact-name duplicates.
    Same name + same custom label + same group_id → merge.

  Phase 2 (--review or --use-llm): potential aliases.
    Heuristic finds short-form vs long-form candidates (a name that is
    a strict prefix or suffix of another, with overlapping label
    classes); the user confirms via LLM or by reviewing the printed
    list. Never auto-applied.

Usage:
    # Dry-run, exact-name duplicates only
    python backend/scripts/dedupe_graph.py --graph-id <gid>

    # Apply phase 1 merges (safe)
    python backend/scripts/dedupe_graph.py --graph-id <gid> --apply

    # Print phase 2 candidates for manual review
    python backend/scripts/dedupe_graph.py --graph-id <gid> --review
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from neo4j import GraphDatabase  # noqa: E402


def _normalize(name: str) -> str:
    return " ".join(name.lower().strip().split())


def _custom_labels(labels: List[str]) -> List[str]:
    return [l for l in (labels or []) if l != "Entity"]


def find_exact_duplicates(driver, graph_id: str) -> List[Tuple[Dict, List[Dict]]]:
    """Return (canonical, [duplicates]) groups where every entity has the
    same normalised name + same custom label + same group_id.

    Canonical = the node with most relationships (and oldest if tied), so
    merging preserves the most-connected node's uuid.
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
        key = (_normalize(r["name"] or ""), "/".join(sorted(labels)))
        if not key[0]:
            continue
        buckets.setdefault(key, []).append(r)

    groups: List[Tuple[Dict, List[Dict]]] = []
    for items in buckets.values():
        if len(items) <= 1:
            continue
        # Canonical = most connected, then oldest
        items.sort(key=lambda x: (-x["rel_count"], str(x["created_at"] or "")))
        canonical, dupes = items[0], items[1:]
        groups.append((canonical, dupes))
    return groups


def find_alias_candidates(driver, graph_id: str) -> List[Tuple[Dict, Dict]]:
    """Return (long_form, short_form) candidates for review only.

    Conservative heuristic: short_form's tokens are a strict prefix or
    strict suffix of long_form's tokens. NOT applied automatically —
    requires user confirmation because shared tokens can be coincidence
    (a single shared surname does not imply two named people are the
    same person).
    """
    cypher = (
        "MATCH (n:Entity) WHERE n.group_id = $gid "
        "RETURN n.uuid AS uuid, n.name AS name, "
        "       [l IN labels(n) WHERE l <> 'Entity'] AS labels"
    )
    with driver.session() as s:
        rows = [dict(r) for r in s.run(cypher, gid=graph_id)]

    rows.sort(key=lambda r: -len(r["name"] or ""))
    candidates: List[Tuple[Dict, Dict]] = []
    seen: set = set()
    for i, long_node in enumerate(rows):
        if long_node["uuid"] in seen:
            continue
        long_norm = _normalize(long_node["name"] or "")
        long_tok = long_norm.split()
        if len(long_tok) < 2:
            continue
        for j in range(i + 1, len(rows)):
            short_node = rows[j]
            if short_node["uuid"] in seen:
                continue
            short_norm = _normalize(short_node["name"] or "")
            if not short_norm:
                continue
            short_tok = short_norm.split()
            # Exact-name duplicates handled by phase 1 — skip here
            if short_norm == long_norm:
                continue
            # Different custom label class → skip
            l_types = set(_custom_labels(long_node["labels"]))
            s_types = set(_custom_labels(short_node["labels"]))
            if l_types and s_types and not (l_types & s_types):
                continue
            # Strict prefix or strict suffix only
            is_prefix = long_tok[: len(short_tok)] == short_tok
            is_suffix = long_tok[-len(short_tok):] == short_tok
            if not (is_prefix or is_suffix):
                continue
            candidates.append((long_node, short_node))
            seen.add(short_node["uuid"])
    return candidates


def merge_into_canonical(driver, canonical_uuid: str, dupe_uuid: str) -> None:
    """Move all relationships from dupe to canonical, then delete dupe."""
    with driver.session() as s:
        # Try APOC first (one-shot, preserves attributes properly)
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
        # Fallback: pure Cypher in 3 passes
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--graph-id", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="Apply phase-1 (exact-name) merges. Default is dry-run.")
    ap.add_argument("--review", action="store_true",
                    help="Also print phase-2 alias candidates for manual review.")
    ap.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    ap.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    ap.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD"))
    args = ap.parse_args()

    if not args.neo4j_password:
        print("NEO4J_PASSWORD not set", file=sys.stderr)
        return 2

    with GraphDatabase.driver(
        args.neo4j_uri, auth=(args.neo4j_user, args.neo4j_password)
    ) as driver:
        # Phase 1
        groups = find_exact_duplicates(driver, args.graph_id)
        total_dupes = sum(len(d) for _, d in groups)
        print(f"Phase 1 — exact-name duplicates: {len(groups)} groups, "
              f"{total_dupes} duplicate nodes to merge")
        for canonical, dupes in groups:
            tag = "/".join(_custom_labels(canonical["labels"]) or ["Entity"])
            print(f"  ({tag}) {canonical['name']!r}  "
                  f"<- {len(dupes)} duplicate(s)  "
                  f"[canonical has {canonical['rel_count']} rels]")

        if args.apply and groups:
            print()
            for canonical, dupes in groups:
                for d in dupes:
                    print(f"  merging duplicate {d['uuid'][:8]}...  "
                          f"into {canonical['name']!r}")
                    merge_into_canonical(driver, canonical["uuid"], d["uuid"])
            print(f"\nApplied: {total_dupes} merges.")
        elif groups:
            print("\nDry-run. Re-run with --apply to merge.")

        if args.review:
            cands = find_alias_candidates(driver, args.graph_id)
            print(f"\nPhase 2 — alias candidates (manual review only): "
                  f"{len(cands)}")
            for long_node, short_node in cands:
                tag = "/".join(_custom_labels(long_node["labels"]) or ["Entity"])
                print(f"  ({tag}) {short_node['name']!r:35s} -> "
                      f"{long_node['name']!r}")
            if cands:
                print("\nThese are NOT auto-merged because shared tokens "
                      "can be coincidence (a single shared surname does "
                      "not imply two named people are the same individual). "
                      "Review and merge manually with Cypher if you confirm "
                      "any are true aliases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
