"""
Repair prompt (docs/master_plan/DETERMINISM_AND_VALIDATION.md §5's "The
repair prompt"). Live-wired by app/core/orchestrator.py's `_repair_node`,
which builds the actual per-request repair text via build_repair_prompt()
below; this module holds the fixed instructional preamble that never
changes between requests. One attempt only; a second failure goes to the
deterministic fallback planner (app/core/fallback.py), never a second repair.
"""
from app.models.schemas import RepairedPlannerOutput
from app.prompts._base import PromptSpec

REPAIR_SYSTEM_PROMPT = """Your previous output failed validation. Fix ONLY the problems
listed below and return the corrected object. Change nothing else - do not re-plan,
re-rank, or restate parts of the output that passed validation.

CONSTRAINTS
- Use only listing_ids that already appeared in this conversation's tool observations.
- Do not re-rank anything. Do not add or remove days beyond what was already there,
  unless a listed failure specifically requires it (e.g. day_count).
- Do not restate any cost or distance number yourself - call the relevant tool
  (estimate_costs, build_day_plan, check_budget) again and copy its result verbatim.
- Return only the corrected structured object. No prose outside it.
"""

REPAIR_SPEC = PromptSpec(
    name="repair",
    version="1.0.0",
    system=REPAIR_SYSTEM_PROMPT,
    output_schema=RepairedPlannerOutput,
)


def build_repair_prompt(failures: list[str]) -> str:
    """Assembles the per-request repair prompt: the fixed preamble plus this
    call's specific, precise failure list - never a vague "invalid output"."""
    failure_lines = "\n".join(f"- {f}" for f in failures)
    return f"{REPAIR_SYSTEM_PROMPT}\nFAILURES\n{failure_lines}\n"


# The finalization-call variant (app/core/react.py's `finalize_system` param)
# - REPAIR_SYSTEM_PROMPT's "call the relevant tool again" line is exactly
# the kind of tool-mandating language that pulled Gemini/Groq toward
# attempting a phantom tool call at the toolless finalization step live
# (2026-09-02/03) - see app/core/react.py's docstring. This variant drops
# that line; if a repair loop genuinely called a tool to get a fresh
# number, that observation is already in the conversation to copy from.
def build_repair_finalize_system(failures: list[str]) -> str:
    failure_lines = "\n".join(f"- {f}" for f in failures)
    return f"""Your previous output failed validation. Fix ONLY the problems listed below and
return the corrected object. Change nothing else - do not re-plan, re-rank, or restate parts of
the output that passed validation. No tools are available in this message - never attempt to call
one; none exist in this turn.

CONSTRAINTS
- Use only listing_ids that already appeared in this conversation's tool observations.
- Do not re-rank anything. Do not add or remove days beyond what was already there, unless a
  listed failure specifically requires it (e.g. day_count).
- Do not restate any cost or distance number yourself - use only values already present in a tool
  observation above, copied verbatim.
- Return only the corrected structured object. No prose outside it.

FAILURES
{failure_lines}
"""
