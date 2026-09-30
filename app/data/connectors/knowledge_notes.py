"""
Team-written knowledge notes -> the RAG knowledge base (migration 0013),
same destination as knowledge_wikivoyage.py's Wikivoyage pages, for facts
that are high-stakes, wrong on Wikivoyage, or missing from it entirely
(the ETA visa process, the Poya-day alcohol ban, temple dress code, tuk-tuk
fares, emergency numbers). Each note in app/data/knowledge/*.md is hand-
written and checked against an official source, recorded in its own front
matter - not scraped, so there is no politeness/robots concern here.

    ---
    title: Sri Lanka Electronic Travel Authorization (ETA)
    source_url: https://www.eta.gov.lk/slvisa/
    district:                # blank/omitted = national, or a real district.name
    last_verified: 2026-09-30
    ---
    ## Section heading
    Body text, in Markdown headings - app/rag/chunking.py splits on these
    the same way it splits Wikivoyage's "== Heading ==" wikitext.

Cadence "monthly" purely so `--due-only` eventually re-checks it if a note
is edited without bumping last_verified by hand; in practice this connector
is usually triggered manually right after editing a note.

    python -m app.data.connectors.knowledge_notes
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any, Optional

import yaml

from app.data.connectors.base import District, fetch_all_districts
from app.rag.ingest import DocumentInput, run_ingest

logger = logging.getLogger(__name__)

NOTES_DIR = Path(__file__).resolve().parent.parent / "knowledge"
SOURCE = "team_note"

NAME = "knowledge_notes"
CADENCE = "monthly"
REQUIRES_KEY = False
SCOPE = "global"


def parse_note(path: Path) -> dict[str, Any]:
    """{title, source_url, district, last_verified, text} from one note
    file's YAML front matter + Markdown body. Raises ValueError on a
    malformed note (no front matter, missing title) - a broken note file
    is a bug to fix, not something to silently skip."""
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise ValueError(f"{path.name}: missing YAML front matter (must start with '---')")

    _, front, body = raw.split("---", 2)
    meta = yaml.safe_load(front) or {}
    if not meta.get("title"):
        raise ValueError(f"{path.name}: front matter is missing 'title'")

    return {
        "title": meta["title"],
        "source_url": meta.get("source_url"),
        "district_name": meta.get("district") or None,   # "" in YAML -> None too
        "last_verified": meta.get("last_verified"),        # yaml parses an unquoted date to a real date object
        "text": body.strip(),
    }


class KnowledgeNotesConnector:
    name = NAME
    cadence = CADENCE
    requires_key = REQUIRES_KEY
    scope = SCOPE

    async def fetch(self, district: Optional[District]) -> list[dict[str, Any]]:
        if not NOTES_DIR.is_dir():
            return []
        districts_by_name = {d.name: d.id for d in await asyncio.to_thread(fetch_all_districts)}

        raw = []
        for path in sorted(NOTES_DIR.glob("*.md")):
            try:
                note = parse_note(path)
            except ValueError as e:
                logger.error(f"knowledge_notes: skipping {path.name} - {e}")
                continue
            district_id = None
            if note["district_name"]:
                district_id = districts_by_name.get(note["district_name"])
                if district_id is None:
                    logger.warning(
                        f"knowledge_notes: {path.name} names unknown district "
                        f"{note['district_name']!r} - stored as national-level instead"
                    )
            raw.append({**note, "source_ref": path.stem, "district_id": district_id})
        return raw

    def normalize(self, raw: list[dict[str, Any]], district: Optional[District]) -> list[DocumentInput]:
        docs = []
        for note in raw:
            last_verified = note["last_verified"]
            docs.append(DocumentInput(
                source=SOURCE,
                source_ref=note["source_ref"],
                title=note["title"],
                url=note["source_url"],
                license="internal",
                district_id=note["district_id"],
                content_hash=hashlib.sha256(note["text"].encode("utf-8")).hexdigest(),
                text=note["text"],
                last_verified=last_verified.isoformat() if last_verified else None,
            ))
        return docs

    def upsert(self, rows: list[DocumentInput]) -> int:
        written, embedded = run_ingest(rows)
        logger.info(f"knowledge_notes: {written} chunk(s) written, {embedded} embedded")
        return written


async def run() -> None:
    connector = KnowledgeNotesConnector()
    raw = await connector.fetch(None)
    docs = connector.normalize(raw, None)
    written = connector.upsert(docs)
    for doc in docs:
        print(f"  {doc.title!r} ({doc.district_id or 'national'}, verified {doc.last_verified})")
    print(f"[SUCCESS] {len(docs)} note(s) loaded, {written} new/changed chunk(s) written.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
