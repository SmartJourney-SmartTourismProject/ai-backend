"""
Splits a document's plain text (already section-marked by whichever
connector fetched it - "== Stay safe ==" style headings from Wikivoyage's
wikitext, or a team-written note's markdown headings) into retrieval-sized
chunks, each carrying the section it came from for citations.

Pure, no I/O - takes text in, returns chunk dicts out. Connectors call this;
app/rag/embeddings.py and the ingest pipeline decide what happens to the
result.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Long enough to carry real context for the LLM to answer from, short
# enough that irrelevant material doesn't dilute a match and a handful of
# retrieved chunks stay well under the "answer" purpose's token budget
# (app/core/llm.py).
CHUNK_CHARS = 1_000
CHUNK_OVERLAP = 150

# Wikivoyage connector marks sections as "== Heading ==" (its own wikitext
# convention, kept through extraction rather than invented here); a
# team-written note uses plain markdown "## Heading". Both recognized so
# both sources chunk the same way.
_SECTION_HEADING = re.compile(r"^(?:={2,4}\s*(.+?)\s*={2,4}|#{1,4}\s+(.+))$", re.MULTILINE)


@dataclass
class RawChunk:
    section: str | None
    content: str
    chunk_index: int


def _split_into_sections(text: str) -> list[tuple[str | None, str]]:
    """[(heading_or_None, body), ...]. Text before the first heading (the
    lead paragraph) is kept under section=None rather than dropped."""
    matches = list(_SECTION_HEADING.finditer(text))
    if not matches:
        return [(None, text.strip())] if text.strip() else []

    sections: list[tuple[str | None, str]] = []
    lead = text[:matches[0].start()].strip()
    if lead:
        sections.append((None, lead))

    for i, m in enumerate(matches):
        heading = m.group(1) or m.group(2)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((heading.strip(), body))
    return sections


def _split_paragraphs(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def _pack(paragraphs: list[str], max_chars: int, overlap: int) -> list[str]:
    """Greedily packs paragraphs into ~max_chars windows. A single
    paragraph longer than max_chars is kept whole rather than cut
    mid-sentence - an oversized chunk answers a question better than a
    truncated one, and it's rare (Wikivoyage paragraphs run short)."""
    if not paragraphs:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        added_len = len(para) + (2 if current else 0)
        if current and current_len + added_len > max_chars:
            chunks.append("\n\n".join(current))
            # Carry the tail of the previous chunk forward as overlap, so a
            # fact split across a chunk boundary is still fully present in
            # at least one chunk.
            tail = chunks[-1][-overlap:] if overlap else ""
            current = [tail, para] if tail else [para]
            current_len = len(tail) + (2 if tail else 0) + len(para)
        else:
            current.append(para)
            current_len += added_len

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def chunk_document(text: str, breadcrumb: str,
                   max_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[RawChunk]:
    """`breadcrumb` (e.g. "Kandy") is prefixed onto every chunk's embedded
    text - not stored in `content` itself, since a short section like "Get
    in: By train." embeds ambiguously on its own but unambiguously as
    "Kandy › Get in: By train.". Callers embed `breadcrumb + " › " +
    section + ": " + content`, not `content` alone; `content` itself stays
    breadcrumb-free so it renders cleanly as an answer's quoted passage."""
    index = 0
    out: list[RawChunk] = []
    for section, body in _split_into_sections(text):
        for piece in _pack(_split_paragraphs(body), max_chars, overlap):
            out.append(RawChunk(section=section, content=piece, chunk_index=index))
            index += 1
    return out


def embedding_text(breadcrumb: str, chunk: RawChunk) -> str:
    """What actually gets embedded - see chunk_document's docstring."""
    parts = [breadcrumb]
    if chunk.section:
        parts.append(chunk.section)
    return " › ".join(parts) + ": " + chunk.content
