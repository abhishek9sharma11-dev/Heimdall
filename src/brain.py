"""
The brain: persona, LLM, and RAG.

- PERSONA_PROMPT defines the bot's voice. Edit this carefully — the
  bot's quality is bounded by how well you describe yourself here.
- ClaudeClient wraps the Anthropic API.
- RAGRetriever pulls relevant context from your knowledge base.
"""
from __future__ import annotations

import asyncio
import json
import logging
import pickle
from pathlib import Path
from typing import List, Optional

import numpy as np
from openai import AsyncOpenAI

from .config import settings

log = logging.getLogger(__name__)


# ============================================================
# PERSONA — EDIT THIS to match your voice
# ============================================================

PERSONA_PROMPT = """You are {bot_name}, an AI co-host and generative AI mentor on {host_name}'s live Zoom webinar. You help attendees understand AI topics while {host_name} presents.

Who you are: You are {host_name}'s AI assistant, not {host_name} himself. If asked, say clearly: I am {host_name}'s AI co-host.

Your voice and style:
- Explain everything like you are talking to a 10 year old. Simple words, no jargon.
- No formatting in your response. No bullet points, no headers, no markdown, no bold, no lists.
- Never use em dashes in any form.
- Reply in exactly 2 short plain text lines, then add 1 line with a simple example or reference.
- Do not number your lines. Just write them naturally.
- Warm and friendly. Direct. No corporate language.

How to reply:
- Line 1: answer the question simply in one sentence.
- Line 2: add one supporting thought or why it matters.
- Line 3: give one concrete example or reference, starting with "For example," or "Think of it like,"
- Address the person by first name if you know it.
- If you have context from {host_name}'s knowledge base below, use it naturally.
- If you do not have a confident answer on a specific fact about {host_name}, say so and offer to flag it for him after the session.

Hard rules:
- Never make up facts about {host_name} such as his pricing, plans, or personal opinions.
- Never commit on {host_name}'s behalf. Always say let me flag that for {host_name}.
- Never use em dashes anywhere in your reply.
- Never use bullet points, numbered lists, or any markdown formatting.
- If the question is medical, legal, or financial, give only general framing and suggest a professional.
- Never engage with hostile or off-topic messages. Say noted, happy to help with anything webinar-related.

Topics you can speak on:
{topics_yes}

Topics to defer to {host_name}:
- Anything personal such as family, relationships, health, or schedule
- Pricing or business decisions
- Specific commitments
- Strong opinions on people or contested topics
{topics_no_extra}

Context from {host_name}'s knowledge base:
{rag_context}

Writing style reference:
{writing_samples}

Now reply to this attendee message. Plain text only. 2 lines plus 1 example line. No em dashes. No formatting.

{sender_name}: {question}

{bot_name}:"""


# ============================================================
# Customize these for yourself
# ============================================================

PERSONA_CONFIG = {
    "host_name": "the host",
    "topics_yes": (
        "AI tools and how to use them in everyday work. "
        "Building newsletters and audiences. "
        "Productivity, prompting, and workflow automation. "
        "Anything covered in past sessions or knowledge base content."
    ),
    "topics_no_extra": (
        "Specific client work or unannounced projects."
    ),
    "writing_samples": (
        "The goal is to explain things simply and clearly, like a mentor talking to someone brand new. "
        "Keep it plain, friendly, and practical. No fancy words. "
        "Every answer should feel like a helpful friend explaining something over coffee, not a textbook."
    ),
}


# ============================================================
# Claude client
# ============================================================

# Free models tried in order — first one that isn't rate-limited wins
FREE_MODEL_FALLBACKS = [
    "google/gemma-4-31b-it:free",
    "openai/gpt-oss-20b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
    "google/gemma-4-26b-a4b-it:free",
]


def _is_local_llm(base_url: str) -> bool:
    u = (base_url or "").lower()
    return any(h in u for h in ("127.0.0.1", "localhost", "0.0.0.0"))


class ClaudeClient:
    def __init__(self) -> None:
        # Ollama / LM Studio accept any non-empty key; OpenRouter needs a real one.
        api_key = settings.openrouter_api_key or (
            "ollama" if _is_local_llm(settings.openrouter_base_url) else ""
        )
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=settings.openrouter_base_url,
        )
        self.primary = settings.anthropic_model
        self.local = _is_local_llm(settings.openrouter_base_url)
        if self.local:
            log.info("LLM local endpoint %s model=%s", settings.openrouter_base_url, self.primary)

    async def reply(
        self,
        sender_name: str,
        question: str,
        rag_context: str,
    ) -> str:
        prompt = PERSONA_PROMPT.format(
            bot_name=settings.bot_display_name,
            host_name=PERSONA_CONFIG["host_name"],
            topics_yes=PERSONA_CONFIG["topics_yes"],
            topics_no_extra=PERSONA_CONFIG["topics_no_extra"],
            writing_samples=PERSONA_CONFIG["writing_samples"],
            rag_context=rag_context or "(no relevant context found in the knowledge base)",
            sender_name=sender_name,
            question=question,
        )

        # Local Ollama: only try the configured model (OpenRouter free fallbacks don't apply).
        if self.local:
            models = [self.primary]
        else:
            models = [self.primary] + [m for m in FREE_MODEL_FALLBACKS if m != self.primary]
        last_err: Exception = RuntimeError("no models available")
        for model in models:
            try:
                resp = await self.client.chat.completions.create(
                    model=model,
                    max_tokens=300,
                    messages=[{"role": "user", "content": prompt}],
                )
                if model != self.primary:
                    log.warning("used fallback model %s", model)
                return (resp.choices[0].message.content or "").strip()
            except Exception as e:
                msg = str(e)
                low = msg.lower()
                # OpenRouter can fail a model in a few "retry with different model" ways:
                # - 429 / rate-limited
                # - 404 / "No endpoints found for <model>"
                if "429" in msg or "rate" in low:
                    last_err = e
                    await asyncio.sleep(0.5)
                    continue
                if "404" in msg or "no endpoints found" in low or "not found" in low:
                    last_err = e
                    continue
                raise
        log.error("all models failed (last error): %s", last_err)
        return "sorry, I'm having trouble connecting right now. Please try again in a moment."


# ============================================================
# RAG: simple FAISS-based retrieval over markdown files
# ============================================================

class RAGRetriever:
    """Loads vector store built by scripts/ingest_knowledge.py."""

    def __init__(self) -> None:
        self.index = None
        self.chunks: List[dict] = []
        self.embedder = None
        self._load()

    def _load(self) -> None:
        store = settings.vector_store_path
        idx_file = store / "index.faiss"
        meta_file = store / "chunks.pkl"

        if not idx_file.exists() or not meta_file.exists():
            log.warning(
                "Vector store not built yet. Run scripts/ingest_knowledge.py. "
                "Bot will operate without RAG context."
            )
            return

        try:
            import faiss
            from sentence_transformers import SentenceTransformer

            self.index = faiss.read_index(str(idx_file))
            with open(meta_file, "rb") as f:
                self.chunks = pickle.load(f)
            self.embedder = SentenceTransformer(settings.embedding_model)
            log.info("rag loaded", extra={"chunks": len(self.chunks)})
        except Exception as e:
            log.exception("failed to load rag store: %s", e)

    def retrieve(self, query: str, top_k: Optional[int] = None) -> str:
        """Returns a single string of formatted context, or empty string."""
        if self.index is None or self.embedder is None:
            return ""

        k = top_k or settings.rag_top_k
        q_vec = self.embedder.encode([query], normalize_embeddings=True)
        distances, indices = self.index.search(np.array(q_vec, dtype="float32"), k)

        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx == -1 or idx >= len(self.chunks):
                continue
            # cosine similarity threshold (with normalized embeddings, distance ~ 2-2*sim)
            if dist > 1.5:  # very loose match
                continue
            chunk = self.chunks[idx]
            results.append(f"From {chunk['source']}:\n{chunk['text']}")

        return "\n\n---\n\n".join(results) if results else ""
