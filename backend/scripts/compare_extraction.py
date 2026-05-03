#!/usr/bin/env python3
"""
Side-by-side extraction comparison: Zep Cloud vs Graphiti+Neo4j.

Reads recent batches from a real simulation's actions.jsonl, ingests the
identical text into both backends, then queries each backend to count
entities and relations created. Outputs a markdown report.

Usage:
    # both backends configured in .env
    python backend/scripts/compare_extraction.py \\
        --simulation-id <sim_id> \\
        --batches 10 \\
        --zep-graph-id <existing-or-new> \\
        --graphiti-graph-id <new>

Requires:
    MEMORY_BACKEND is ignored here — this script forces both backends.
    ZEP_API_KEY must be set.
    NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD must be set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow running as `python backend/scripts/compare_extraction.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config  # noqa: E402
from app.services.zep_graph_memory_updater import (  # noqa: E402
    AgentActivity,
    ZepGraphMemoryUpdater,
)
from app.services.graphiti_graph_memory_updater import (  # noqa: E402
    GraphitiGraphMemoryUpdater,
)
from app.services.zep_tools import ZepToolsService  # noqa: E402
from app.services.graphiti_tools import GraphitiToolsService  # noqa: E402


@dataclass
class BackendResult:
    name: str
    total_seconds: float = 0.0
    batches_sent: int = 0
    nodes: List[Dict[str, Any]] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


def load_batches(simulation_id: str, n_batches: int, batch_size: int = 5) -> List[List[AgentActivity]]:
    """Load N batches of activities from the simulation's actions logs."""
    sim_dir = Path(Config.OASIS_SIMULATION_DATA_DIR) / simulation_id
    candidate_files = [
        sim_dir / "twitter" / "actions.jsonl",
        sim_dir / "reddit" / "actions.jsonl",
        sim_dir / "actions.jsonl",
    ]
    activities: List[AgentActivity] = []
    for path in candidate_files:
        if not path.exists():
            continue
        platform = "reddit" if "reddit" in str(path) else "twitter"
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "event_type" in data:
                    continue
                if data.get("action_type") == "DO_NOTHING":
                    continue
                activities.append(
                    AgentActivity(
                        platform=platform,
                        agent_id=data.get("agent_id", 0),
                        agent_name=data.get("agent_name", ""),
                        action_type=data.get("action_type", ""),
                        action_args=data.get("action_args", {}),
                        round_num=data.get("round", 0),
                        timestamp=data.get("timestamp", datetime.now().isoformat()),
                    )
                )

    batches: List[List[AgentActivity]] = []
    for i in range(0, len(activities), batch_size):
        chunk = activities[i : i + batch_size]
        if len(chunk) == batch_size:
            batches.append(chunk)
        if len(batches) >= n_batches:
            break
    return batches


def run_zep(graph_id: str, batches: List[List[AgentActivity]]) -> BackendResult:
    result = BackendResult(name="zep")
    try:
        updater = ZepGraphMemoryUpdater(graph_id)
        updater.start()
        start = time.time()
        for batch in batches:
            for activity in batch:
                updater.add_activity(activity)
        # Flush + give Zep a moment to extract.
        updater.stop()
        result.total_seconds = time.time() - start
        result.batches_sent = len(batches)
        # Wait a bit for server-side processing to settle.
        time.sleep(15)
        # Read back nodes/edges.
        tools = ZepToolsService()
        result.nodes = [n.to_dict() for n in tools.get_all_nodes(graph_id)]
        result.edges = [e.to_dict() for e in tools.get_all_edges(graph_id)]
    except Exception as e:
        result.error = str(e)
    return result


def run_graphiti(graph_id: str, batches: List[List[AgentActivity]]) -> BackendResult:
    result = BackendResult(name="graphiti")
    tools: Optional[GraphitiToolsService] = None
    try:
        updater = GraphitiGraphMemoryUpdater(graph_id)
        updater.start()
        start = time.time()
        for batch in batches:
            for activity in batch:
                updater.add_activity(activity)
        updater.stop()
        result.total_seconds = time.time() - start
        result.batches_sent = len(batches)
        tools = GraphitiToolsService()
        result.nodes = [n.to_dict() for n in tools.get_all_nodes(graph_id)]
        result.edges = [e.to_dict() for e in tools.get_all_edges(graph_id)]
    except Exception as e:
        result.error = str(e)
    finally:
        if tools is not None:
            try:
                tools.close()
            except Exception:
                pass
    return result


def overlap_metrics(a: List[Dict[str, Any]], b: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    set_a = {(item.get(key) or "").lower().strip() for item in a if item.get(key)}
    set_b = {(item.get(key) or "").lower().strip() for item in b if item.get(key)}
    if not set_a and not set_b:
        return {"a": 0, "b": 0, "common": 0, "jaccard": 0.0}
    common = len(set_a & set_b)
    union = len(set_a | set_b) or 1
    return {
        "a": len(set_a),
        "b": len(set_b),
        "common": common,
        "jaccard": round(common / union, 3),
    }


def write_report(out_path: Path, n_batches: int, zep: BackendResult, gra: BackendResult) -> None:
    name_overlap = overlap_metrics(zep.nodes, gra.nodes, "name")
    fact_overlap = overlap_metrics(zep.edges, gra.edges, "fact")

    lines = [
        "# Zep vs Graphiti — extraction comparison",
        "",
        f"- Batches sent: {n_batches}",
        f"- Generated at: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Latency",
        "",
        "| Backend | Ingest seconds | Batches sent | Error |",
        "|---|---:|---:|---|",
        f"| Zep | {zep.total_seconds:.1f} | {zep.batches_sent} | {zep.error or '-'} |",
        f"| Graphiti | {gra.total_seconds:.1f} | {gra.batches_sent} | {gra.error or '-'} |",
        "",
        "## Volume",
        "",
        "| Backend | Nodes | Edges |",
        "|---|---:|---:|",
        f"| Zep | {len(zep.nodes)} | {len(zep.edges)} |",
        f"| Graphiti | {len(gra.nodes)} | {len(gra.edges)} |",
        "",
        "## Overlap",
        "",
        "| Metric | Zep | Graphiti | Common | Jaccard |",
        "|---|---:|---:|---:|---:|",
        f"| Entity names | {name_overlap['a']} | {name_overlap['b']} | "
        f"{name_overlap['common']} | {name_overlap['jaccard']} |",
        f"| Edge facts | {fact_overlap['a']} | {fact_overlap['b']} | "
        f"{fact_overlap['common']} | {fact_overlap['jaccard']} |",
        "",
        "## Sample entities (top 10 each)",
        "",
        "### Zep",
        "",
    ]
    for n in zep.nodes[:10]:
        lines.append(f"- **{n.get('name', '?')}** ({', '.join(n.get('labels', []))})")
    lines.append("")
    lines.append("### Graphiti")
    lines.append("")
    for n in gra.nodes[:10]:
        lines.append(f"- **{n.get('name', '?')}** ({', '.join(n.get('labels', []))})")
    lines.append("")
    lines.append("## Sample facts (top 10 each)")
    lines.append("")
    lines.append("### Zep")
    lines.append("")
    for e in zep.edges[:10]:
        lines.append(f"- {e.get('fact', '?')}")
    lines.append("")
    lines.append("### Graphiti")
    lines.append("")
    for e in gra.edges[:10]:
        lines.append(f"- {e.get('fact', '?')}")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare Zep vs Graphiti extraction")
    parser.add_argument("--simulation-id", required=True, help="Simulation id (folder under uploads/simulations/)")
    parser.add_argument("--batches", type=int, default=10, help="Number of batches to ingest")
    parser.add_argument("--batch-size", type=int, default=5, help="Activities per batch")
    parser.add_argument("--zep-graph-id", required=True, help="Zep graph_id")
    parser.add_argument("--graphiti-graph-id", required=True, help="Graphiti group_id")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("backend/scripts/_compare_report.md"),
        help="Output markdown path",
    )
    parser.add_argument("--skip-zep", action="store_true", help="Skip Zep ingestion")
    parser.add_argument("--skip-graphiti", action="store_true", help="Skip Graphiti ingestion")
    args = parser.parse_args()

    if errors := Config.validate():
        print("Config errors:")
        for e in errors:
            print(f"  - {e}")
        # Allow partial config if user is skipping a backend.

    print(f"Loading {args.batches} batches from simulation {args.simulation_id}...")
    batches = load_batches(args.simulation_id, args.batches, args.batch_size)
    if not batches:
        print("No batches loaded — check simulation id / actions.jsonl path", file=sys.stderr)
        return 2
    print(f"Loaded {len(batches)} batches")

    zep_result = (
        BackendResult(name="zep", error="skipped")
        if args.skip_zep
        else run_zep(args.zep_graph_id, batches)
    )
    gra_result = (
        BackendResult(name="graphiti", error="skipped")
        if args.skip_graphiti
        else run_graphiti(args.graphiti_graph_id, batches)
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_report(args.out, len(batches), zep_result, gra_result)
    print(f"Report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
