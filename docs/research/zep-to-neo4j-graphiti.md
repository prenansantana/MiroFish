# Zep Cloud → Graphiti+Neo4j (optional self-hosted backend)

## TL;DR

This fork supports two memory backends, selectable via `MEMORY_BACKEND`:

- **`zep`** (default) — Zep Cloud SaaS, the upstream behavior. No code change.
- **`graphiti`** — Graphiti + Neo4j Community, self-hosted in Docker.

Both code paths coexist permanently. Default is `zep` so anyone cloning
this fork without setting env vars sees the same behavior as upstream.

When to consider `graphiti`:

- Volume above ~2 simulations/month (the Zep `Flex` plan starts at $125/mo).
- Concerns about Zep's product trajectory (Zep CE was deprecated in 2025).

When `zep` is the right call:

- One simulation/month or less — Zep Free tier (1k credits/mo) covers it for $0.
- No appetite for operating a Neo4j container on the host.

## Stack we ship in `graphiti` mode

Single paid vendor (Anthropic). Everything else is local:

| Component | What we use | Why |
|---|---|---|
| Knowledge graph DB | Neo4j 5 Community in Docker | only Graphiti-supported DB that's GPLv3 + free |
| LLM (extraction) | **Anthropic Claude Sonnet 4.6** via `AnthropicClient` | testado em PT-BR, captura relações implícitas (ex: `REPRESENTS_CONTINUITY_OF`, `GOVERNS`) que modelos "mini" perdem |
| Embeddings | **BGE-M3 via sentence-transformers** in-process | top-tier multilingual em PT (MTEB-PT). 100% local, zero $/mês. PyTorch (~500MB) puxado como dep transitiva. |
| Reranker | **`LLMReranker` reusing the same Sonnet client** | zero ops, ~$1/mês, frequentemente bate cross-encoder genérico em domínio rico |

The only outbound API key the project pays for is Anthropic. The
extraction LLM call is the entire variable cost.

## Why this option exists

Original ask was "swap Zep for Postgres." That turned out to be wrong-shaped:

- Zep does not store your graph in a generic key-value way; its core value is
  an **LLM-driven entity & relation extraction pipeline** (Graphiti) plus
  hybrid semantic+BM25 search on top of a knowledge graph.
- Replacing only the storage layer (Postgres + pgvector) means rewriting
  extraction. Replicating Graphiti-quality extraction is the bulk of Zep's
  product — months of work, not days.
- Zep Community Edition (self-hosted Zep image) was **deprecated in
  April 2025**, so "self-host the same thing" is no longer an option.
- Graphiti, the open-source extraction engine, is published by Zep itself
  but does not run on Postgres. Supported backends are Neo4j, FalkorDB,
  Amazon Neptune, and previously Kuzu (Kuzu was archived in October 2025
  after Apple acquired it).

So the real trade is **Zep Cloud SaaS** vs **Graphiti+Neo4j self-hosted**,
not Zep vs Postgres.

## Pricing (April 2026)

Sources: [getzep.com/pricing](https://www.getzep.com/pricing/).

| Tier | Monthly | Credits | Per-credit |
|---|---|---|---|
| Free | $0 | 1,000 | — |
| Flex | $125 | 50,000 | $0.0025 |
| Flex Plus | $375 | 200,000 | $0.00188 |
| Enterprise | Custom | — | — |

Credits = 1 per Episode ≤350 bytes, +1 per additional 350 bytes. Search
calls also consume credits.

This project's Episodes are batches of 5 actions described in CN/PT
(~100 bytes per action), so each batch is ~500 bytes ≈ 2 credits, plus
search overhead — call it **~5 credits per Episode**.

## Cost projection for typical use

A representative run profile for this fork's expected workload is
**200–500 agents × 30 rounds × 4 simulation models**. One full "study"
hits roughly **60,000 credits** for the 500-agent variant on Zep, and
~3,000 Graphiti episodes (batched 5 activities per episode).

Self-host LLM cost is computed against **Anthropic Sonnet 4.6**
(~$3/1M input, $15/1M output) at the Graphiti pipeline's typical
4k input + 1.5k output per Episode (~$0.034/episode). Embeddings are
zero (sentence-transformers local). Reranker adds ~$1/mo total.

| Use case | Zep monthly | Graphiti monthly (Sonnet) | Delta |
|---|---:|---:|---:|
| 1 small sim (200 ag × 30 r) | $125 (Flex floor) | $10 VPS + ~$40 LLM = **$50** | −$75 |
| 1 study (4 models × 500 ag) | $375 (Flex Plus) | $10 + ~$100 = **$110** | −$265 |
| 5 studies/mo (200 ag) | $675 (Flex Plus + overage) | $15 + ~$510 = **$525** | −$150 |
| 10 studies/mo (500 ag) | $1,500+ | $20 + ~$1,020 = **$1,040** | −$460 |

The Free→Flex cliff is the killer. The first simulation that exceeds 1k
credits puts you on a $125/mo floor regardless of usage that month.

> Choosing the model: this project tested Haiku 4.5, Opus 4.7, and
> Sonnet 4.6 against the same 5-episode PT-BR fixture. Haiku missed
> implicit relations (e.g. failed to capture continuity-of-government
> framings expressed only via co-reference and tense). Opus extracted
> the richest graph but is ~10× the price of Sonnet. **Sonnet 4.6 is the
> sweet spot** — captures the same nuanced relations Opus does at
> ~20% of the cost. If extraction quality is the bottleneck on a
> specific run, swap `LLM_MODEL_NAME` to `claude-opus-4-7` for that
> run only.

## Data residency note

For deployments that ingest sensitive content, the architecture keeps
most data on infrastructure the operator controls:

- Knowledge graph (nodes, edges, embeddings): local Neo4j only.
- Embeddings: local (sentence-transformers in-process), never leaves
  the host.
- Episode text: travels to the LLM provider for extraction (the only
  hop out). To eliminate that final hop, see "Fully local" below.

## Vendor risk

Zep retired the Community Edition in April 2025 with additional
feature retirements in February 2026. That's two product reshapes in
~12 months. Neo4j Community Edition has been GPLv3 open source since
2010 and is operated by tens of thousands of organisations;
structurally more stable for any multi-year deployment window.

## Architecture

The factory at `backend/app/services/_memory_backend.py` dispatches on
`Config.MEMORY_BACKEND`:

```
                +-----------------+
call sites ---> | get_tools_      |
                | service()       | -> ZepToolsService     (MEMORY_BACKEND=zep)
                | get_entity_     |    or
                | reader()        | -> GraphitiToolsService (MEMORY_BACKEND=graphiti)
                | get_memory_     |
                | updater_        | -> ZepEntityReader / GraphitiEntityReader
                | manager()       | -> ZepGraphMemoryManager / GraphitiGraphMemoryManager
                +-----------------+
```

Both backend implementations expose the same public surface
(search_graph, get_all_nodes, get_all_edges, …, insight_forge,
panorama_search, quick_search, interview_agents). Call sites are
backend-agnostic.

### Mapping

| Zep concept | Graphiti equivalent |
|---|---|
| `Zep(api_key)` | `Graphiti(neo4j_uri, neo4j_user, neo4j_password)` |
| `client.graph.add(graph_id, type='text', data)` | `await graphiti.add_episode(name, episode_body, source=EpisodeType.text, source_description, reference_time, group_id=graph_id)` |
| `client.graph.search(graph_id, query, scope, reranker)` | `await graphiti.search(query, group_ids=[graph_id], num_results)` (+ `_search` with `NODE_HYBRID_SEARCH_RRF` for nodes) |
| `client.graph.node.get_by_graph_id(graph_id, uuid_cursor)` | Cypher: `MATCH (n:Entity) WHERE n.group_id = $gid RETURN n SKIP $skip LIMIT $limit` |
| `client.graph.edge.get_by_graph_id(...)` | Cypher: `MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) WHERE r.group_id = $gid ...` |
| `client.graph.node.get(uuid_)` | Cypher: `MATCH (n:Entity {uuid: $uuid}) RETURN n LIMIT 1` |
| `client.graph.node.get_entity_edges(node_uuid)` | Cypher: `MATCH (n:Entity {uuid: $uuid})-[r]-() RETURN r` |
| `graph_id` (string per simulation) | `group_id` property on nodes/edges (same string) |

### Async bridging

Graphiti is fully async. The MiroFish backend is sync (Flask + threading).
`_memory_backend.py` provides:

- `AsyncRunner` — long-lived asyncio loop in a daemon thread. Used by
  `GraphitiGraphMemoryUpdater` so each per-batch `add_episode` is one
  loop submission rather than a per-call `asyncio.run()`.
- `run_coro(coro)` — one-shot helper for ad-hoc calls from request
  handlers.

### Client wiring

`_graphiti_clients.py` is the single place that decides which LLM,
embedder, and reranker get passed to `Graphiti(...)`. Both adapters
call the `make_graphiti(uri, user, password)` factory; nothing else
touches Anthropic / embedder specifics.

- `SentenceTransformerEmbedder` — loads the configured model via
  `sentence_transformers` on first call, caches it for the process
  lifetime. Fail-soft: returns zero vectors on error so ingest doesn't
  crash; the `_local_search` keyword fallback in `graphiti_tools.py`
  still serves search even if vectors are bad.
- `LLMReranker` — wraps the Graphiti LLM client (`AnthropicClient` or
  `OpenAIClient`) and ranks via a JSON prompt. Caps at 50 passages
  per call and 400 chars per passage.
- `_build_llm_client()` switches on `Config.GRAPHITI_LLM_PROVIDER`
  (default `anthropic`). Set to `openai` to use Graphiti's default
  OpenAI client instead.

### Shared state

The dataclasses (`SearchResult`, `NodeInfo`, `EdgeInfo`, `EntityNode`,
`InsightForgeResult`, `PanoramaResult`, `AgentInterview`,
`InterviewResult`, `AgentActivity`) live in the `zep_*.py` modules and
are re-imported by the `graphiti_*.py` adapters. They describe shapes,
not behavior, so reuse is safe.

The orchestrators (`insight_forge`, `panorama_search`, `quick_search`,
`get_simulation_context`, `get_entity_summary`, `get_graph_statistics`,
`get_entities_by_type`, `interview_agents`, sub-query generation,
interview question generation, interview summary) are lifted near
verbatim from `zep_tools.py` because they consume only the low-level
primitives + the LLM client.

## Setup

### To use Zep Cloud (default — no setup needed beyond env)

```bash
# .env
ZEP_API_KEY=...
# MEMORY_BACKEND defaults to "zep" — leave unset
```

### To use Graphiti+Neo4j

```bash
# .env (minimal)
MEMORY_BACKEND=graphiti
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<choose a password>

# LLM — Anthropic Sonnet 4.6 (recommended)
LLM_API_KEY=sk-ant-...
LLM_BASE_URL=https://api.anthropic.com/v1/
LLM_MODEL_NAME=claude-sonnet-4-6
LLM_JSON_MODE=none           # Anthropic via OpenAI-compat path doesn't support response_format

# Graphiti provider switch — leave 'anthropic' for the recommended stack
GRAPHITI_LLM_PROVIDER=anthropic

# Embeddings local — default model is BAAI/bge-m3 (1024-d, top-tier
# multilingual). The model is downloaded automatically by
# sentence-transformers on first use (~2GB).
SENTENCE_TRANSFORMER_MODEL=BAAI/bge-m3
```

```bash
# 1. Bring up Neo4j Community
NEO4J_PASSWORD=$(grep ^NEO4J_PASSWORD= .env | cut -d= -f2) \
  docker compose -f backend/docker-compose.neo4j.yml up -d

# 2. Install Python deps (pulls torch + sentence-transformers)
cd backend
pip install -r requirements.txt

# 3. Run as usual — the BGE-M3 model is downloaded on first request
#    (~2GB, cached under ~/.cache/huggingface).
python run.py
```

### Verifying the install

```bash
# Compare backends side-by-side on real episodes from a previous simulation
python backend/scripts/compare_extraction.py \
    --simulation-id <existing-sim-id> \
    --batches 10 \
    --zep-graph-id zep_compare_$(date +%s) \
    --graphiti-graph-id gra_compare_$(date +%s)

# Measure Graphiti cost on real volume
python backend/scripts/measure_cost.py \
    --simulation-id <existing-sim-id> \
    --episodes 50 \
    --graph-id measure_$(date +%s)
```

`measure_cost.py` patches `openai.Completions.create` to capture
`response.usage`, so you get real token counts (not estimates) and a
USD figure based on the price table in the script.

## Future paths (out of scope here)

These are listed for reference only — they are not implemented in this
branch.

### Fully local (no outbound API at all)

The current stack already runs embeddings local. To close the LLM
hop too:

1. **Move the extraction LLM local.** Run Ollama/vLLM/llama.cpp on
   the host with a competent model (Llama 3.3 70B, Qwen 2.5 72B, or
   similar). Point `LLM_BASE_URL` at the local server, set
   `GRAPHITI_LLM_PROVIDER=openai` (Ollama exposes OpenAI-compat
   endpoint), and update `LLM_MODEL_NAME`.

2. **Optional: swap `LLMReranker` for a real cross-encoder.** Stand
   up `text-embeddings-inference` (Hugging Face) with
   `BAAI/bge-reranker-v2-m3` and implement a `TEIReranker` that calls
   `/rerank`. Replace `LLMReranker(llm)` with `TEIReranker()` in
   `_graphiti_clients.make_graphiti`.

For 70B+ models, expect 3-5x slower extraction vs Sonnet on a
typical workstation. Throughput, not quality, is the bottleneck of
fully-local LLM extraction at this project's volume.

### Operational hardening

- Neo4j backup automation (`neo4j-admin database dump` on a cron).
- CI matrix against both backends.
- Export/import between backends for users who want to migrate
  existing graph_ids.

### i18n of episode templates

The natural-language descriptions sent to the memory backend now live
under `episode.*` in `locales/{zh,pt-br,pt-pt,en}.json`. The `zh`
wording is preserved verbatim so default behavior is byte-identical
for upstream Chinese users; PT-BR/PT-PT/EN renderings match the
target locale's natural register and reduce token cost on non-CN
deployments by ~25-30%.

InsightForge result rendering (`zep_tools.py:to_text`), profile
generator progress messages, and skip-profiles status messages were
also moved to localized keys (`report.insightForge*`,
`progress.profileCompleted`, `progress.profilesReused`) so reports
generated under `LOCALE=pt-br` no longer mix Chinese headers with
Portuguese content.

## Operational findings from a real run (May 2026)

End-to-end smoke (162 entities, 10 sim rounds, 4-section report)
under the `graphiti` backend with Sonnet 4.6 yielded these calibration
points worth recording before the next iteration of optimizations.

### Anthropic prompt-cache effective threshold

Anthropic's docs cite a 1024-token minimum for cache-eligible system
blocks on Sonnet. Empirically on `claude-sonnet-4-6` the floor is
closer to **~2000 tokens** — sub-threshold writes silently report
`cache_creation_input_tokens=0` with no error. Calibration table:

| System tokens | `cache_creation` | `cache_read` (next call) |
|---|---|---|
| 1016 | 0 | 0 |
| 2516 | 2508 | unreliable |
| 5016+ | ~5008 | ~5008 |

Practical impact for this codebase: Graphiti's extraction prompts
peak at ~1788 tokens (system part) with default ontology, ~3500
with rich custom ontology. The profile-generator system is ~150
tokens (uncacheable). Reranker prompts vary by passage count.

### Cache hits in production traffic

Single-run measurement (162 profiles + 10-round sim + report,
~5.36M input tokens total in the prep window):

- `cache_write_5m`: ~299K (5.6% of input)
- `cache_read`: ~337K (6.3% of input)
- Direct dollar savings: ~$0.91 (3.4% of input cost)

The win is real but small because the hot paths
(`oasis_profile_generator`, Graphiti `add_episode`) wrap most
context inside the **user message**, not the system block. Splitting
user messages on natural boundaries (`<TEXT>...</TEXT>`,
`<CURRENT_MESSAGE>...`) was prototyped and shelved — fragile across
graphiti-core upgrades, low ROI.

`MIN_CACHE_CHARS` is set to **4000** in
`backend/app/services/_graphiti_clients.py` and
`backend/app/utils/llm_client.py`. That's below Anthropic's effective
floor for English (~1000 tokens at 4 chars/token) but well above for
Chinese (~2700 tokens at 1.5 chars/token), so the `cache_control`
flag fires universally and Anthropic decides which actually cache.

### Profile reuse across simulations of the same project

`oasis_profile_generator` does **not** read
`simulation_requirement` — profiles depend only on KG entities. A
project running Modelo A then Modelo B/C/D therefore wastes ~$26 of
LLM calls on each repeat unless we reuse profiles.

`SimulationManager.prepare_simulation` now detects a sibling sim
under the same `project_id` whose `reddit_profiles.json` +
`twitter_profiles.csv` are complete and matching `entities_count`.
When found, it copies the files and skips stage 2 entirely.
Helper: `SimulationManager._find_reusable_profiles`.

### Zombie state cleanup at startup

If the backend dies mid-prepare or mid-run (debug-reload, OOM,
`kill -9`), the in-memory `TaskManager` forgets the task but
`state.json` keeps `status=preparing` or `running` indefinitely.
The UI then polls them as if alive, showing a permanent "0% / in
progress" spinner.

`SimulationManager.cleanup_zombie_states()` runs on every
`create_app()` and rewrites such states to `failed` with an
explanatory error. Pid liveness is checked for `running` states via
`os.kill(pid, 0)`.

### Defensive subprocess termination

The simulation runner's monitor thread relies on
`while process.poll() is None` to detect natural exit, then cleans
up its `_processes` dict. If the monitor thread itself raises before
reaching that loop's normal exit, the subprocess can outlive the
backend by hours (observed: pid alive 1h25min after the sim was
already marked completed). The `finally` block now defensively
calls `_terminate_process` if `process.poll() is None`.

### What would actually reduce cost on the next branch

Ordered by impact on a typical 162-entity, 10-round, 4-section run:

1. **Swap `LLMReranker` for TEI / `bge-reranker-v2-m3` local** —
   each search currently pays a 1-2s LLM round-trip for ranking;
   ReACT runs 5-15 searches per section. Estimated savings: $10-15
   per report, plus 30-60s wall time.
2. **Reuse profiles** — already in this branch, $26 per additional
   scenario on the same project.
3. **Reduce report ReACT depth** — current default lets the agent
   take many search iterations per section; capping aggressively
   trades nuance for cost.

### Empirical rounds-per-run sweet spot under Sonnet 4.6

A second wave of measurements (May 7 2026) on a 96-agent KG under
the project's default Anthropic tier (450k input-tokens/min) found
a hard ceiling:

| Rounds | Wall time   | Cost  | camel-oasis "rate limit exhausted" | Memory writes dropped | Outcome |
|--------|-------------|-------|------------------------------------|-----------------------|---------|
| 10     | ~9 min      | ~$13  | 0                                  | 0                     | Clean, no degradation |
| 15     | ~30 min     | ~$30  | 18 agent decisions lost            | 0                     | Memory drained eventually; some agents missed rounds |
| 20     | OOM at 14   | ~$20  | n/a                                | n/a                   | Runner subprocess killed by SIGKILL (memory pressure) |

Two sources of throttling stack at scale:

1. **Anthropic per-minute input-token cap.** With 96 agents emitting
   per-round LLM decisions plus search, reranker, and memory-write
   calls, the 450k/min budget saturates around round 12-13 of a
   15-round run.
2. **camel-oasis upstream retry** has only 3 attempts with ~1-5s
   backoff — far too short for a per-minute window. It silently
   drops the agent's action when the third attempt fails. The
   downstream effect: rounds finish with fewer captured actions
   than intended; reports built off those rounds are less rich.

The Graphiti memory updater in this fork was hardened to handle
this — see "Anthropic rate limit headers" below — but the camel-
oasis side is upstream code and would need a wrapper to fully
shield it.

**Practical recommendation:** at this Anthropic tier with this fork's
Sonnet-everywhere setup, cap simulations at **10 rounds** per run.
Use the multi-scenario flow (Modelo A/B/C/D under the same project
with `simulation_requirement` overrides) to get breadth instead of
depth. The skip-profiles fast-path means each additional scenario
is ~$13-15, not $40.

### Anthropic rate-limit aware backoff in the Graphiti memory updater

`_send_batch_activities` now splits retries into two budgets:
generic errors (3 attempts, linear 2/4/6s) and rate-limit errors
(up to 10 attempts, waiting per `retry-after` or `anthropic-ratelimit-
input-tokens-reset` when present, 60s default otherwise, hard cap
120s). Detection covers both shapes that surface in practice:

- SDK exceptions with `status_code=429` and `response.headers`
- Re-raised strings containing `rate_limit_error`, `rate limit
  exceeded`, or `429` (used when graphiti-core wraps the error)

Operator-facing log line so the parking is visible (not stuck):
```
WARNING Anthropic rate limit on Graphiti batch (5 reddit
        activities) — waiting 60s before retry (1/10)
```

Measured on a 15-round run: 3 wait events, 0 batches dropped.

### graphiti_core driver lazy-import deadlock

Observed once: a 15-round sim ran to completion with zero memory
writes because `GraphitiGraphMemoryUpdater.__init__` instantiated
`Graphiti(...)` from a worker thread, which lazy-imported
`graphiti_core.driver.neo4j_driver` while another thread was
concurrently inside a related module. Python's `_ModuleLock`
detected the circular wait and aborted with
`deadlock detected by _ModuleLock('graphiti_core.driver.driver')`.

The runner caught the exception, set `_graph_memory_enabled=False`,
and ran the sim silently without persisting actions. The report
generated afterward was based only on the original PDF-derived KG.

Two-part fix:

1. `create_app()` now eager-imports `graphiti_core`,
   `graphiti_core.driver`, `graphiti_core.driver.neo4j_driver`, and
   `Graphiti` when `MEMORY_BACKEND=graphiti`. Touching the modules
   on the main thread primes `sys.modules` so the worker thread's
   later use is a no-op import.
2. `SimulationRunner.start_simulation` retries `create_updater`
   once after 100ms when the exception mentions `_ModuleLock` /
   `deadlock detected` (transient cases), and re-raises as
   `RuntimeError` on persistent failure so /start returns HTTP 500
   instead of letting the sim run blind to memory.

The user opted into `enable_graph_memory_update`; failing loud is
the right default.

### Zombie state recovery

Both `state.json` (preparing/running) and `run_state.json`
(starting/running) can be left in a non-terminal status when the
backend crashes, gets killed, or is reload-restarted by debug mode
mid-flow. The UI then polls them as live, showing a permanent
"0% / in progress" spinner with no recovery path other than
editing files by hand.

Two complementary cleanups:

- `SimulationManager.cleanup_zombie_states()` runs on
  `create_app()` — sweeps all simulation states, checks pid liveness
  (signal-0 probe) for `running`, and rewrites stuck states to
  `failed` with an explanatory error.
- `SimulationRunner.start_simulation` repeats the pid liveness
  check on the recorded `process_pid` for `run_state.json` shaped
  zombies; if the pid is dead/absent it logs a warning, marks the
  run failed, and proceeds with a fresh start instead of rejecting.

### Per-sim simulation_requirement override

`SimulationState` carries an optional `simulation_requirement`
field; when present, `prepare_simulation` (config gen) and the
`/report/generate` + `/report/chat` endpoints all prefer it over
the project's value. This lets the same project run Modelo A/B/C/D
scenarios on the same KG and skip-profiles fast-path without
rewriting the project's stated requirement.

API surface for the override:

```http
POST /api/simulation/create
{
  "project_id": "...",
  "graph_id": "...",
  "simulation_requirement": "...optional override..."
}
```

The Project hub UI in Step1GraphBuild surfaces this via a "+ New
simulation" modal pre-filled with the project's current value;
the user edits it (or keeps it as-is) and the override is only
sent when it differs from the project's default.

## Files of interest

| File | Purpose |
|---|---|
| `backend/docker-compose.neo4j.yml` | Neo4j 5 Community + APOC, persistent volumes |
| `backend/app/config.py` | `MEMORY_BACKEND`, `NEO4J_URI/USER/PASSWORD`, `SENTENCE_TRANSFORMER_MODEL`, `GRAPHITI_LLM_PROVIDER` |
| `backend/app/services/_memory_backend.py` | Factory dispatcher (zep vs graphiti) + `AsyncRunner` |
| `backend/app/services/_graphiti_clients.py` | `SentenceTransformerEmbedder`, `LLMReranker`, `make_graphiti()` |
| `backend/app/services/graphiti_graph_memory_updater.py` | Ingestion adapter |
| `backend/app/services/graphiti_tools.py` | Tools / search / orchestrators |
| `backend/app/services/graphiti_entity_reader.py` | Read-side Cypher |
| `backend/scripts/compare_extraction.py` | Side-by-side comparison |
| `backend/scripts/measure_cost.py` | Real token-count cost measurement |
| `locales/{zh,pt-br,pt-pt,en}.json` | `episode.*` templates |
