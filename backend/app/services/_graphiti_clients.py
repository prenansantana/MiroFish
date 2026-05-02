"""
Graphiti client factory: Anthropic LLM + local embeddings + LLM-based reranker.

Why this composition:
  - LLM (Anthropic Sonnet 4.6): testado, top-tier para extração nuançada em PT-BR.
  - Embeddings: 100% local. Default Ollama (HTTP, sem dep Python pesada).
    Fallback `sentence_transformers` em-process (em Mac com macOS 26.3+ tem
    bug Metal/bfloat no Ollama runner — workaround: trocar EMBED_PROVIDER).
  - Reranker (mesmo LLM via prompt): zero ops adicional, custo desprezível
    (~$1/mês), e em domínio rico (política) frequentemente bate cross-encoder
    genérico. Pode ser trocado por TEI/bge-reranker depois sem mudar mais nada.

Single provider story: o usuário paga apenas para Anthropic. Embeddings rodam
locais. Neo4j Community é grátis. Custo recorrente = só os tokens Sonnet.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

import httpx
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client.anthropic_client import AnthropicClient
from graphiti_core.llm_client.client import LLMClient as GraphitiLLMClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_client import OpenAIClient
from graphiti_core.prompts.models import Message

from ..config import Config
from ..utils.logger import get_logger

logger = get_logger('mirofish.graphiti_clients')


# ---------------------------------------------------------------------------
# Embeddings — Ollama HTTP
# ---------------------------------------------------------------------------


class OllamaEmbedder(EmbedderClient):
    """
    Embeddings via local Ollama (`/api/embed`).

    Setup pré-requisito (uma vez por host):
        $ ollama pull bge-m3   # ou outro modelo configurado em OLLAMA_EMBED_MODEL

    Roda 100% local. Default model BGE-M3 produz vetores 1024-d, top-tier em
    benchmarks multilingual (MTEB-PT, MIRACL).
    """

    DEFAULT_DIM = 1024  # BGE-M3 dimension; truncated/padded if model differs

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (base_url or Config.OLLAMA_BASE_URL).rstrip('/')
        self.model = model or Config.OLLAMA_EMBED_MODEL
        self._client = httpx.AsyncClient(timeout=timeout)

    async def create(self, input_data) -> List[float]:
        """
        Always returns a single 1D embedding (List[float]).

        Mirrors graphiti_core.embedder.openai.OpenAIEmbedder.create which
        ignores extra inputs and returns `result.data[0].embedding`. So
        when given list[str], we encode the first one only — Graphiti's
        contract for `create` is single-embedding regardless of input form.
        Use `create_batch` for true batch encoding.
        """
        if isinstance(input_data, list) and input_data and isinstance(input_data[0], str):
            embeddings = await self._embed([input_data[0]])
            return embeddings[0]
        if isinstance(input_data, str):
            embeddings = await self._embed([input_data])
            return embeddings[0]
        # Tokenized inputs (rare path) — best-effort: stringify
        embeddings = await self._embed([str(input_data)])
        return embeddings[0]

    async def create_batch(self, input_data_list: List[str]) -> List[List[float]]:
        return await self._embed(input_data_list)

    async def _embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        try:
            response = await self._client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": texts},
            )
            response.raise_for_status()
            data = response.json()
            embeddings = data.get("embeddings") or []
            if not embeddings:
                logger.warning(f"Ollama returned empty embeddings for {len(texts)} inputs")
                return [[0.0] * self.DEFAULT_DIM for _ in texts]
            return embeddings
        except Exception as e:
            logger.error(f"Ollama embedding call failed: {e}")
            # Fail-soft: return zero vectors so the pipeline keeps running.
            return [[0.0] * self.DEFAULT_DIM for _ in texts]


# ---------------------------------------------------------------------------
# Reranker — LLM-based (uses the same Graphiti LLM client)
# ---------------------------------------------------------------------------


class LLMReranker(CrossEncoderClient):
    """
    Reranks passages by asking the LLM to order them by relevance to the query.

    Cheaper than running a separate cross-encoder model:
      - No torch / sentence-transformers / TEI container.
      - Reuses the Anthropic SDK already configured for extraction.
      - In rich domains (e.g. politics) often beats generic cross-encoders
        because the LLM understands nuanced context.

    Trade-off: 1-2s extra per search. Irrelevant for Graphiti search (the
    semantic stage already takes seconds). If volume scales 100x and tokens
    become an issue, swap for TEI without touching anything else.
    """

    MAX_PASSAGES_PER_CALL = 50
    MAX_PASSAGE_CHARS = 400

    def __init__(self, llm_client: GraphitiLLMClient) -> None:
        self.llm = llm_client

    async def rank(
        self, query: str, passages: List[str]
    ) -> List[Tuple[str, float]]:
        if not passages:
            return []
        if len(passages) == 1:
            return [(passages[0], 1.0)]

        # Cap to avoid flooding context if Graphiti hands us a huge list.
        capped = passages[: self.MAX_PASSAGES_PER_CALL]

        numbered = "\n".join(
            f"[{i}] {p[: self.MAX_PASSAGE_CHARS]}" for i, p in enumerate(capped)
        )
        system_prompt = (
            "You are a relevance ranker. Given a query and numbered passages, "
            "return a JSON object ranking passages by relevance to the query, "
            "most relevant first. Format strictly: "
            '{"ranked_indices": [<int>, ...]}'
        )
        user_prompt = f"Query: {query}\n\nPassages:\n{numbered}"

        try:
            response = await self.llm.generate_response(
                messages=[
                    Message(role="system", content=system_prompt),
                    Message(role="user", content=user_prompt),
                ],
                response_model=None,  # we parse JSON manually for portability
            )
            ranked_indices = self._parse_ranked_indices(response, len(capped))
        except Exception as e:
            logger.warning(f"LLMReranker failed, returning original order: {e}")
            ranked_indices = list(range(len(capped)))

        # Score: linear from 1.0 (top) toward 0.0 (bottom of the ranked list).
        n = max(len(ranked_indices), 1)
        scored: List[Tuple[str, float]] = []
        for rank, idx in enumerate(ranked_indices):
            if 0 <= idx < len(capped):
                scored.append((capped[idx], 1.0 - rank / n))

        # Append any passages that the LLM dropped, with a residual score.
        seen = set(ranked_indices)
        for i, p in enumerate(capped):
            if i not in seen:
                scored.append((p, 0.0))

        # If we capped, append the leftover passages with score 0 so callers
        # still see them in some order.
        if len(passages) > len(capped):
            for p in passages[len(capped):]:
                scored.append((p, 0.0))

        return scored

    @staticmethod
    def _parse_ranked_indices(response, n: int) -> List[int]:
        # `response` may already be a dict (some LLM clients parse JSON for us)
        # or a string we need to extract JSON from.
        if isinstance(response, dict):
            data = response
        else:
            text = str(response)
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise ValueError("no JSON object in LLM response")
            data = json.loads(match.group(0))

        indices = data.get("ranked_indices") or data.get("ranking") or []
        if not isinstance(indices, list):
            raise ValueError("ranked_indices is not a list")
        return [int(i) for i in indices if isinstance(i, (int, str))]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _build_llm_client() -> GraphitiLLMClient:
    """Choose Anthropic vs OpenAI based on Config.GRAPHITI_LLM_PROVIDER."""
    cfg = LLMConfig(
        api_key=Config.LLM_API_KEY,
        model=Config.LLM_MODEL_NAME,
        small_model=Config.LLM_MODEL_NAME,
    )
    if Config.GRAPHITI_LLM_PROVIDER == 'anthropic':
        return AnthropicClient(config=cfg)
    # Default OpenAI client; honours OPENAI_BASE_URL / api key env if present.
    return OpenAIClient(config=cfg)


def _build_embedder() -> EmbedderClient:
    """
    Pick embedder by Config.EMBED_PROVIDER (default `ollama`).

    Use `sentence_transformers` in environments where Ollama's llama runner
    crashes (e.g. macOS 26.3+ has a known bfloat/half Metal shader bug
    that affects Ollama 0.21-0.22 — workaround is in-process via PyTorch
    MPS).
    """
    provider = Config.EMBED_PROVIDER
    if provider == 'sentence_transformers':
        return SentenceTransformerEmbedder()
    # default
    return OllamaEmbedder()


class SentenceTransformerEmbedder(EmbedderClient):
    """
    Embeddings via sentence-transformers in-process.

    Use as a workaround when Ollama's llama runner can't load the embedding
    model (macOS 26.3 + Ollama <= 0.22 Metal shader bug). On Apple Silicon
    PyTorch's MPS backend handles bfloat correctly.

    Trade-off: pulls torch as a hard dep (~500MB), takes ~5-10s on first
    init to load the model, occupies ~2GB RAM while alive.
    """

    def __init__(self, model: Optional[str] = None) -> None:
        self.model_name = model or Config.SENTENCE_TRANSFORMER_MODEL
        self._model = None

    def _ensure_loaded(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # lazy
            logger.info(f"Loading sentence-transformers model {self.model_name!r}...")
            self._model = SentenceTransformer(self.model_name)
            logger.info("sentence-transformers model loaded")
        return self._model

    async def create(self, input_data) -> List[float]:
        """
        Always returns a single 1D embedding (List[float]).

        See OllamaEmbedder.create for the rationale — Graphiti's contract
        is single-embedding from `create` regardless of input form.
        """
        if isinstance(input_data, list) and input_data and isinstance(input_data[0], str):
            return self._encode([input_data[0]])[0]
        if isinstance(input_data, str):
            return self._encode([input_data])[0]
        return self._encode([str(input_data)])[0]

    async def create_batch(self, input_data_list: List[str]) -> List[List[float]]:
        return self._encode(input_data_list)

    def _encode(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        try:
            model = self._ensure_loaded()
            arr = model.encode(texts, normalize_embeddings=True)
            return [vec.tolist() for vec in arr]
        except Exception as e:
            logger.error(f"sentence-transformers encode failed: {e}")
            dim = getattr(self._model, 'get_sentence_embedding_dimension', lambda: 1024)() or 1024
            return [[0.0] * dim for _ in texts]


def make_graphiti(
    uri: str,
    user: str,
    password: str,
    *,
    llm_client: Optional[GraphitiLLMClient] = None,
    embedder: Optional[EmbedderClient] = None,
    cross_encoder: Optional[CrossEncoderClient] = None,
) -> Graphiti:
    """
    Construct a Graphiti instance wired for this fork's stack:

      LLM       — Anthropic Sonnet 4.6 (or whatever LLM_MODEL_NAME points at)
      Embedder  — Ollama BGE-M3 local
      Reranker  — LLM-based, reusing the same LLM client

    Override any of the three by passing kwargs (used in tests).
    """
    llm = llm_client or _build_llm_client()
    emb = embedder or _build_embedder()
    rerank = cross_encoder or LLMReranker(llm)
    return Graphiti(
        uri=uri,
        user=user,
        password=password,
        llm_client=llm,
        embedder=emb,
        cross_encoder=rerank,
    )
