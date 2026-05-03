#!/usr/bin/env python3
"""
Measure Graphiti extraction cost (tokens + USD) on real episodes.

Approach: monkey-patch the OpenAI client used by Graphiti's LLM client
to capture `response.usage.{prompt,completion}_tokens` on every call.
Sum across N episodes, multiply by published prices for the configured
LLM_MODEL_NAME, and print a summary.

Limitations:
- Pricing table is hard-coded (see `MODEL_PRICES`); add your model if
  it's missing. Defaults to a conservative gpt-4o-mini estimate.
- Counts only completion tokens, not embedding tokens. Graphiti's
  embedding calls are typically <5% of cost, so the bias is small.

Usage:
    python backend/scripts/measure_cost.py \\
        --simulation-id <sim_id> \\
        --episodes 50 \\
        --graph-id measure_cost_$(date +%s)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config  # noqa: E402
from app.services.zep_graph_memory_updater import AgentActivity  # noqa: E402
from app.services.graphiti_graph_memory_updater import (  # noqa: E402
    GraphitiGraphMemoryUpdater,
)


# Prices in USD per 1M tokens, as of 2026-04. Update when needed.
MODEL_PRICES: Dict[str, Dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.150, "output": 0.600},
    "gpt-4o": {"input": 2.500, "output": 10.000},
    "gpt-4-turbo": {"input": 10.000, "output": 30.000},
    "claude-sonnet-4-6": {"input": 3.000, "output": 15.000},
    "claude-opus-4-7": {"input": 15.000, "output": 75.000},
    "qwen-plus": {"input": 0.800, "output": 2.000},
    "qwen-max": {"input": 1.600, "output": 6.400},
}


class TokenTracker:
    """Aggregates input/output tokens across all OpenAI calls."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def record(self, usage: Any) -> None:
        if usage is None:
            return
        with self.lock:
            self.calls += 1
            self.input_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.output_tokens += int(getattr(usage, "completion_tokens", 0) or 0)

    def snapshot(self) -> Dict[str, int]:
        with self.lock:
            return {
                "calls": self.calls,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            }


def install_tracker() -> TokenTracker:
    """Monkey-patch openai client to record usage on every chat completion."""
    from openai.resources.chat.completions import Completions

    tracker = TokenTracker()

    original_create = Completions.create

    def wrapped_create(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        response = original_create(self, *args, **kwargs)
        try:
            tracker.record(getattr(response, "usage", None))
        except Exception:
            pass
        return response

    Completions.create = wrapped_create  # type: ignore[assignment]

    # Also patch async path if Graphiti uses it.
    try:
        from openai.resources.chat.completions import AsyncCompletions

        original_acreate = AsyncCompletions.create

        async def wrapped_acreate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            response = await original_acreate(self, *args, **kwargs)
            try:
                tracker.record(getattr(response, "usage", None))
            except Exception:
                pass
            return response

        AsyncCompletions.create = wrapped_acreate  # type: ignore[assignment]
    except Exception:
        pass

    return tracker


def load_episodes(
    simulation_id: str, n_episodes: int, batch_size: int
) -> List[List[AgentActivity]]:
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

    episodes: List[List[AgentActivity]] = []
    for i in range(0, len(activities), batch_size):
        chunk = activities[i : i + batch_size]
        if len(chunk) == batch_size:
            episodes.append(chunk)
        if len(episodes) >= n_episodes:
            break
    return episodes


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure Graphiti extraction cost")
    parser.add_argument("--simulation-id", required=True)
    parser.add_argument("--episodes", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--graph-id", required=True, help="Graphiti group_id (use a fresh value)")
    args = parser.parse_args()

    if errors := Config.validate():
        # Allow LLM_API_KEY missing if using local. Just report.
        for e in errors:
            print(f"WARNING: {e}", file=sys.stderr)

    print(f"Installing OpenAI usage tracker...")
    tracker = install_tracker()

    print(f"Loading {args.episodes} episodes from simulation {args.simulation_id}...")
    episodes = load_episodes(args.simulation_id, args.episodes, args.batch_size)
    if not episodes:
        print("No episodes loaded.", file=sys.stderr)
        return 2
    print(f"Loaded {len(episodes)} episodes")

    print(f"Starting Graphiti updater for graph_id={args.graph_id}...")
    updater = GraphitiGraphMemoryUpdater(args.graph_id)
    updater.start()

    print(f"Ingesting {len(episodes)} episodes...")
    start = time.time()
    for ep in episodes:
        for act in ep:
            updater.add_activity(act)

    print("Stopping updater (flushes pending batches)...")
    updater.stop()
    elapsed = time.time() - start

    snap = tracker.snapshot()

    model = Config.LLM_MODEL_NAME
    prices = MODEL_PRICES.get(model)
    if prices is None:
        print(f"\nNo price entry for model '{model}'. Add it to MODEL_PRICES.")
        cost_in = cost_out = total_cost = 0.0
    else:
        cost_in = (snap["input_tokens"] / 1_000_000) * prices["input"]
        cost_out = (snap["output_tokens"] / 1_000_000) * prices["output"]
        total_cost = cost_in + cost_out

    print("\n=== RESULT ===")
    print(f"Model:               {model}")
    print(f"Episodes ingested:   {len(episodes)}")
    print(f"Wall time:           {elapsed:.1f}s ({elapsed / max(len(episodes), 1):.2f}s/episode)")
    print(f"OpenAI calls:        {snap['calls']}")
    print(f"Input tokens:        {snap['input_tokens']:,}")
    print(f"Output tokens:       {snap['output_tokens']:,}")
    if prices is not None:
        print(f"Input cost:          ${cost_in:.4f}")
        print(f"Output cost:         ${cost_out:.4f}")
        print(f"Total cost:          ${total_cost:.4f}")
        print(f"Per episode:         ${total_cost / max(len(episodes), 1):.4f}")
        print()
        print("Extrapolations (per simulation):")
        for label, agents, rounds in [
            ("small  (200 ag x 30 rounds, 2 plats)", 200, 30),
            ("large  (500 ag x 30 rounds, 2 plats)", 500, 30),
            ("study  (4 models x 500 ag x 30 r)", 500 * 4, 30),
        ]:
            sim_episodes = (agents * rounds * 2) / args.batch_size
            sim_cost = (total_cost / max(len(episodes), 1)) * sim_episodes
            print(f"  {label:<42s} ~${sim_cost:.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
