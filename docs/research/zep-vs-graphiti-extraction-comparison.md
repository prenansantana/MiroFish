# Zep Cloud vs Graphiti+Neo4j — extraction comparison

Apples-to-apples benchmark of the two memory backends supported by
this fork's `MEMORY_BACKEND` flag. Run on a synthetic PT-BR fixture
of social-media activity (posts, likes, comments, follows) — same
input fed to both backends with empty graphs.

## Setup

- **Input**: 3 batches × 5 activities = 14 unique non-`DO_NOTHING`
  activities, all in PT-BR.
- **Episode templates**: PT-BR locale (`episode.*` keys committed in
  the i18n commit).
- **Both graphs created empty** for this run (Zep `graph.create` +
  Graphiti default), so the numbers reflect only what was extracted
  from these 14 activities — not historical accumulation.

### Zep stack (managed)
- Zep Cloud, default extraction pipeline (Graphiti embedded server-side).
- Custom entity types pre-configured in the Zep project (typed labels
  such as Organisation, Person, Topic, …).

### Graphiti stack (this fork's self-hosted backend)
- LLM: Anthropic Claude **Sonnet 4.6** via `AnthropicClient`.
- Embeddings: BGE-M3 1024-d via `sentence-transformers` in-process.
- Reranker: `LLMReranker` reusing the same Anthropic client.
- Graph DB: Neo4j 5 Community in Docker (default `:Entity` labels — no
  custom EntityType subclasses registered yet).

## Results

### Volume

| Backend | Nodes | Edges |
|---|---:|---:|
| Zep | 10 | 6 |
| Graphiti | 28 | 25 |

Graphiti extracted **2.8× more entities** and **4.2× more relations**
from the same input.

### Entity overlap

| | Zep | Graphiti | Common | Jaccard |
|---|---:|---:|---:|---:|
| Entity names | 10 | 28 | 7 | 0.23 |

The seven common entities matched core actors mentioned in the
fixture; the additional 21 entities Graphiti captured are secondary
referents (organisations, programmes, locations) that Zep's extractor
folded into the descriptions of the primary entities instead of
materialising as separate nodes.

### Sample fact shape

#### Zep — facts in mixed PT/EN

The Zep pipeline frequently switches into English in its summarisation
prompts, producing facts like:

> Person A *disliked a post by* Person B *highlighting legacy
> advancements in health (new regional hospitals), education
> investment, and a robust economy due to high agribusiness
> performance.*

Some are pure PT, some pure EN, some mixed mid-sentence.

#### Graphiti — facts in pure PT-BR

The fork's adapter receives episode templates already in the user's
locale (PT-BR in this run), and Sonnet preserves the language end to
end. Sample facts (paraphrased to abstract from the fixture content):

> *"\<Person A\> consolida \<topic\> com \<programme\> em \<location\>."*
>
> *"Pesquisa da \<Pollster\> aponta \<Person B\> como nome mais citado
> para \<role\>, com \<X\>% de intenção de voto espontâneo."*
>
> *"\<Person C\> é ex-\<Org\> e articula \<action\> com apoio de
> \<Person D\>."*

## Reading the result

**Volume difference comes from extraction depth, not bugs.** Zep's
default extractor under-resolves multi-fact statements (one post can
contain 4-5 distinct relations: who-publishes-what, who-defends-what,
what-is-associated-with-what). Graphiti+Sonnet captures each one as
a separate edge. Both are valid graphs of the same input — Graphiti's
is denser.

**Custom entity types**: Zep had typed labels (Person, Organisation,
…) because the Zep project was configured with schema-typed entities
upstream. The Graphiti adapter in this fork uses Graphiti's default
`:Entity` labels for now. To match, register `EntityType` subclasses
on the Graphiti instance — out of scope for this benchmark, easy to
add later.

**Language consistency**: Graphiti facts stay in the locale of the
input (PT-BR here). Zep mixed PT and EN in some facts because the
upstream Graphiti prompts that ship with Zep normalise toward English
in summarisation. Our fork side-steps that by translating the
`episode.*` templates into the active locale before they ever reach
the LLM.

**Latency**: Zep ingestion API responded in ~1s (queues async server-
side; full processing took ~30s before the graph was queryable).
Graphiti ran fully synchronous in ~67s. End-to-end "graph ready"
times are comparable.

## Reproducing

```bash
# Bring up Neo4j (sentence-transformers runs in-process — no extra service)
docker compose -f backend/docker-compose.neo4j.yml up -d

# Create empty graphs
python -c "from zep_cloud.client import Zep; \
  Zep(api_key='<key>').graph.create(graph_id='compare_zep')"

# Run the side-by-side script
python backend/scripts/compare_extraction.py \
    --simulation-id <some-sim-id> \
    --batches 3 --batch-size 5 \
    --zep-graph-id compare_zep \
    --graphiti-graph-id compare_gra
```

`backend/scripts/compare_extraction.py` reads
`backend/uploads/simulations/<sim-id>/{twitter,reddit}/actions.jsonl`,
ingests the same activities into both backends, and writes a markdown
report.

Cost on a 3-batch run:
- Anthropic Sonnet 4.6: ~$0.10 (3 batches × ~4-5k input + ~1.5k output)
- Zep: ~6 credits (within Free-tier monthly quota)
- Local: zero (Neo4j + sentence-transformers BGE-M3)
