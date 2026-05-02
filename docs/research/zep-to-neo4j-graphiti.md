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
