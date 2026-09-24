"""
PromptSpec - the shared shape every centralized prompt module builds
(docs/master_plan/DETERMINISM_AND_VALIDATION.md §3). Not just a container:
`version` is logged with every call so a prompt change is traceable in
whatever observability exists, and `max_input_chars` guards against a
runaway candidate payload getting silently truncated by the provider
instead of failing loudly here first.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


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

def enforce_max_input_chars(spec: PromptSpec, human: str) -> str:
    """O3 (AI_BACKEND_OPTIMIZATION_PLAN.md): max_input_chars was documented
    ("guards against a runaway candidate payload getting silently truncated
    by the provider instead of failing loudly here first") but never
    actually read anywhere in app/ - a 413 from the provider was the only
    signal a caller ever got. Called at each agent's human-message
    construction site, right after building `human`. Trims rather than
    raises: an oversized payload should still degrade to the best answer the
    model can give from a truncated view (the same "always produce
    something" philosophy as run_react's finalization guarantee), not crash
    the request - but it logs loudly so the truncation is diagnosable
    instead of silently eating context."""
    if len(human) <= spec.max_input_chars:
        return human
    logger.warning(
        f"{spec.name}: human message ({len(human)} chars) exceeds max_input_chars "
        f"({spec.max_input_chars}) - trimming rather than letting the provider "
        f"silently truncate or reject it."
    )
    return human[:spec.max_input_chars]


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
