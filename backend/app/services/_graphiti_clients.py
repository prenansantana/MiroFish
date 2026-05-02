"""
Graphiti client factory: Anthropic LLM + local embeddings + LLM-based reranker.

Why this composition:
  - LLM (Anthropic Sonnet 4.6): top-tier for nuanced PT-BR extraction.
  - Embeddings: BGE-M3 via sentence-transformers in-process. PyTorch's
    MPS backend handles bfloat correctly on Apple Silicon (Ollama 0.21-
    0.22 has a known Metal shader bug on macOS 26.3+; that's why we
    pin sentence-transformers as the local embedder rather than reach
    over HTTP).
  - Reranker (same LLM via prompt): zero extra ops, ~$1/mo, frequently
    beats generic cross-encoders on rich domains. Swappable for TEI /
    bge-reranker later without touching adapters.

Single paid vendor: Anthropic. Embeddings, reranker, and graph DB all
run locally.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

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
# Embeddings — sentence-transformers in-process
# ---------------------------------------------------------------------------


class SentenceTransformerEmbedder(EmbedderClient):
    """
    Embeddings via sentence-transformers in-process.

    Default model is `BAAI/bge-m3` (1024-d, top-tier multilingual on
    MTEB-PT and MIRACL). On Apple Silicon PyTorch uses MPS automatically;
    on Linux x86 it picks CUDA when available, CPU otherwise.

    Trade-offs:
      - First instantiation loads the model (~5-10s, ~2GB RAM).
      - The model is reused across calls — only first request pays
        the load cost.
      - Pulls torch as a hard dep (~500MB), but most projects already
        have it transitively (anthropic / openai SDKs).
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

        Mirrors graphiti_core.embedder.openai.OpenAIEmbedder.create which
        ignores extra inputs and returns `result.data[0].embedding`. When
        given list[str], we encode the first element only — Graphiti's
        contract for `create` is single-embedding regardless of input
        form. Use `create_batch` for true batch encoding.
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


# ---------------------------------------------------------------------------
# Reranker — LLM-based (uses the same Graphiti LLM client)
# ---------------------------------------------------------------------------


class LLMReranker(CrossEncoderClient):
    """
    Reranks passages by asking the LLM to order them by relevance to the query.

    Cheaper than running a separate cross-encoder model:
      - No torch / sentence-transformers / TEI container.
      - Reuses the Anthropic SDK already configured for extraction.
      - In rich domains often beats generic cross-encoders because the
        LLM understands nuanced context.

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
                response_model=None,
            )
            ranked_indices = self._parse_ranked_indices(response, len(capped))
        except Exception as e:
            logger.warning(f"LLMReranker failed, returning original order: {e}")
            ranked_indices = list(range(len(capped)))

        n = max(len(ranked_indices), 1)
        scored: List[Tuple[str, float]] = []
        for rank, idx in enumerate(ranked_indices):
            if 0 <= idx < len(capped):
                scored.append((capped[idx], 1.0 - rank / n))

        seen = set(ranked_indices)
        for i, p in enumerate(capped):
            if i not in seen:
                scored.append((p, 0.0))

        if len(passages) > len(capped):
            for p in passages[len(capped):]:
                scored.append((p, 0.0))

        return scored

    @staticmethod
    def _parse_ranked_indices(response, n: int) -> List[int]:
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
    return OpenAIClient(config=cfg)


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
      Embedder  — BGE-M3 via sentence-transformers in-process
      Reranker  — LLM-based, reusing the same LLM client

    Override any of the three by passing kwargs (used in tests).
    """
    llm = llm_client or _build_llm_client()
    emb = embedder or SentenceTransformerEmbedder()
    rerank = cross_encoder or LLMReranker(llm)
    return Graphiti(
        uri=uri,
        user=user,
        password=password,
        llm_client=llm,
        embedder=emb,
        cross_encoder=rerank,
    )
