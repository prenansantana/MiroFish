"""
LLM client wrapper.

Default path: OpenAI SDK with the configured base_url. Works with
qwen-cloud, OpenAI proper, Ollama, and any other OpenAI-compatible
runtime.

Anthropic path: when LLM_BASE_URL points at api.anthropic.com, the
client switches to the Anthropic Python SDK directly. This unlocks
two things the OpenAI compatibility layer doesn't support:

  1. Prompt caching. System prompts in this project are large
     (3-5k tokens for ontology/profile/report) and repeat across
     many calls (162 profile generations, dozens of report queries).
     Marking the system block with `cache_control: ephemeral` cuts
     billed input tokens by ~85% on repeat calls (cache_read costs
     $0.30/1M vs $3/1M for fresh input on Sonnet 4.6).

  2. Native JSON output via Anthropic's structured outputs, instead
     of the `response_format` parameter which Anthropic's OpenAI
     compat rejects.

The public surface (`chat`, `chat_json`) stays identical so call sites
don't need to know which path they're on.
"""

import json
import re
from typing import Any, Dict, List, Optional

from openai import OpenAI

from ..config import Config
from .logger import get_logger

logger = get_logger('mirofish.llm_client')


def _is_anthropic_endpoint(base_url: str) -> bool:
    return "anthropic" in (base_url or "").lower()


class LLMClient:
    """LLM client. Picks the right SDK at construction time based on
    whether LLM_BASE_URL points at Anthropic or anything else."""

    # Anthropic's docs claim 1024-token minimum for Sonnet, but empirical
    # tests on Sonnet 4.6 show writes start failing below ~2000 tokens.
    # 4000 chars maps to ~1000 EN tokens / ~2700 ZH tokens — below
    # Anthropic's effective floor in EN, comfortably above in ZH (this
    # repo's primary system-prompt language). Below the floor Anthropic
    # silently returns cache_creation_input_tokens=0 with no penalty,
    # so a too-low threshold costs nothing — we just don't get the win.
    # Setting 4000 lets the flag fire on every reasonably-sized prompt
    # and we let Anthropic decide which actually cache.
    MIN_CACHE_CHARS = 4000

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model = model or Config.LLM_MODEL_NAME

        if not self.api_key:
            raise ValueError("LLM_API_KEY 未配置")

        self._is_anthropic = _is_anthropic_endpoint(self.base_url)

        if self._is_anthropic:
            from anthropic import Anthropic  # lazy
            self._anthropic = Anthropic(api_key=self.api_key)
            self.client = None  # not used in Anthropic path
        else:
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            self._anthropic = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None,
    ) -> str:
        """Send messages to the configured LLM and return text."""
        if self._is_anthropic:
            return self._chat_anthropic(
                messages=messages, temperature=temperature, max_tokens=max_tokens
            )
        return self._chat_openai(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        """Send messages and parse the response as a JSON object."""
        # Anthropic native: nudge JSON via system message reinforcement;
        # response_format isn't accepted on the OpenAI-compat path either
        # (LLM_JSON_MODE=none is the project default for that reason).
        response = self.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=(
                {"type": "json_object"}
                if not self._is_anthropic and Config.LLM_JSON_MODE == "json_object"
                else None
            ),
        )
        cleaned = response.strip()
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        cleaned = cleaned.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM返回的JSON格式无效: {cleaned}") from exc

    # ------------------------------------------------------------------
    # OpenAI path (qwen, OpenAI, OpenAI-compat servers)
    # ------------------------------------------------------------------

    def _chat_openai(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
        response_format: Optional[Dict],
    ) -> str:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format
        response = self.client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content
        # Some open models (MiniMax M2.5) embed <think>...</think> blocks.
        return re.sub(r"<think>[\s\S]*?</think>", "", content).strip()

    # ------------------------------------------------------------------
    # Anthropic path with prompt caching
    # ------------------------------------------------------------------

    def _chat_anthropic(
        self,
        messages: List[Dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> str:
        # Split out system messages (Anthropic API takes them in a
        # separate `system` parameter, not interleaved with user/assistant).
        system_blocks: List[Dict[str, Any]] = []
        chat_messages: List[Dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "system":
                if content:
                    system_blocks.append({"type": "text", "text": content})
            else:
                chat_messages.append({"role": role, "content": content})

        # Cache the last system block if it's substantial. The last block
        # is what the prefix-cache lookup keys on; marking just one block
        # is enough — Anthropic caches up to that point.
        if system_blocks:
            longest = max(system_blocks, key=lambda b: len(b["text"]))
            if len(longest["text"]) >= self.MIN_CACHE_CHARS:
                longest["cache_control"] = {"type": "ephemeral"}

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": chat_messages,
        }
        if system_blocks:
            kwargs["system"] = system_blocks

        response = self._anthropic.messages.create(**kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None:
            cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cr = getattr(usage, "cache_read_input_tokens", 0) or 0
            it = getattr(usage, "input_tokens", 0) or 0
            ot = getattr(usage, "output_tokens", 0) or 0
            if cw or cr:
                logger.info(
                    f"anthropic cache hit/write — in={it} out={ot} "
                    f"cache_write={cw} cache_read={cr}"
                )
        parts = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        text = "".join(parts)
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
