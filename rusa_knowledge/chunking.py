"""Deterministic, overlap-aware text chunking for knowledge ingestion."""

from __future__ import annotations

import re

DEFAULT_CHUNK_CHARS = 1200
DEFAULT_OVERLAP_CHARS = 180


def normalize_text(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("document text must be a string")
    text = text.replace("\x00", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_OVERLAP_CHARS,
) -> list[str]:
    """Split text into bounded chunks, preferring paragraph and sentence boundaries."""
    if chunk_size < 128 or chunk_size > 20_000:
        raise ValueError("chunk_size must be between 128 and 20000 characters")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be non-negative and smaller than chunk_size")

    normalized = normalize_text(text)
    if not normalized:
        return []

    chunks: list[str] = []
    start = 0
    length = len(normalized)
    while start < length:
        end = min(start + chunk_size, length)
        if end < length:
            candidates = (
                normalized.rfind("\n\n", start + chunk_size // 2, end),
                normalized.rfind("\n", start + chunk_size // 2, end),
                normalized.rfind(". ", start + chunk_size // 2, end),
                normalized.rfind(" ", start + chunk_size // 2, end),
            )
            boundary = max(candidates)
            if boundary > start:
                end = boundary + (2 if normalized[boundary : boundary + 2] == ". " else 1)

        piece = normalized[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= length:
            break
        next_start = max(start + 1, end - overlap)
        start = next_start

    return chunks
