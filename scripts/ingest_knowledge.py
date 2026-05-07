"""
Build a FAISS vector index from everything in ./knowledge/.

Run whenever you add or edit knowledge content:
    python scripts/ingest_knowledge.py

Outputs to ./vector_store/index.faiss + chunks.pkl.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path
from typing import List

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings  # noqa: E402


CHUNK_SIZE = 800     # chars
CHUNK_OVERLAP = 120


def chunk_text(text: str, source: str) -> List[dict]:
    """Naive paragraph-aware chunking."""
    text = text.strip()
    if not text:
        return []

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        # Try to break on a paragraph boundary if possible
        if end < len(text):
            nl = text.rfind("\n\n", start, end)
            if nl > start + CHUNK_SIZE // 2:
                end = nl
        chunk = text[start:end].strip()
        if chunk:
            chunks.append({"source": source, "text": chunk})
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP
    return chunks


def main() -> int:
    knowledge_dir = settings.knowledge_dir
    store_dir = settings.vector_store_path

    if not knowledge_dir.exists():
        print(f"ERROR: knowledge dir not found: {knowledge_dir}")
        return 1

    files = sorted(
        p for p in knowledge_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in {".md", ".txt"}
    )
    if not files:
        print(f"No .md or .txt files found in {knowledge_dir}")
        return 1

    print(f"Found {len(files)} knowledge files. Chunking...")
    all_chunks: List[dict] = []
    for f in files:
        rel = f.relative_to(knowledge_dir)
        text = f.read_text(encoding="utf-8", errors="replace")
        all_chunks.extend(chunk_text(text, str(rel)))

    print(f"Built {len(all_chunks)} chunks. Loading embedding model...")

    import faiss
    import numpy as np
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(settings.embedding_model)
    print(f"Embedding {len(all_chunks)} chunks...")
    vecs = model.encode(
        [c["text"] for c in all_chunks],
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    vecs = np.array(vecs, dtype="float32")

    dim = vecs.shape[1]
    index = faiss.IndexFlatL2(dim)
    index.add(vecs)

    store_dir.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(store_dir / "index.faiss"))
    with open(store_dir / "chunks.pkl", "wb") as fh:
        pickle.dump(all_chunks, fh)

    print(f"\n✓ Vector store written to {store_dir}/")
    print(f"  {len(all_chunks)} chunks, dim={dim}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
