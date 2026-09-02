"""
PromptSpec - the shared shape every centralized prompt module builds
(docs/master_plan/DETERMINISM_AND_VALIDATION.md §3). Not just a container:
`version` is logged with every call so a prompt change is traceable in
whatever observability exists, and `max_input_chars` guards against a
runaway candidate payload getting silently truncated by the provider
instead of failing loudly here first.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel


@dataclass(frozen=True)
class PromptSpec:
    name: str
    version: str                              # bump on every content change
    system: str
    output_schema: Optional[type[BaseModel]] = None   # None for prompts that aren't structured-output calls (e.g. response narration)
    max_input_chars: int = 24_000


# ─────────────────────────── shared rule blocks ───────────────────────────
# Reused verbatim across multiple prompts so a wording change only needs to
# happen once - copy-pasted rules are exactly how two prompts drift apart.

OUTPUT_ONLY_RULE = "Return only the structured object described above. No prose, no markdown, no text outside it."

NO_INVENTION_RULE = (
    "Never invent a place, price, date, or fact not present in this conversation's tool "
    "observations. If you don't have real data for something, say so explicitly rather than "
    "guessing a plausible-sounding value."
)

SRI_LANKA_ONLY_RULE = (
    "This assistant covers Sri Lanka only. If the destination resolved outside Sri Lanka, "
    "that was already caught before you were called - you will never be asked to plan a trip "
    "outside Sri Lanka."
)
