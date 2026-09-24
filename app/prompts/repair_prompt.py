"""
Repair prompt (docs/master_plan/DETERMINISM_AND_VALIDATION.md §5's "The
repair prompt"). Live-wired by app/core/orchestrator.py's `_repair_node`.
One attempt only; a second validation failure goes to the deterministic
fallback planner (app/core/fallback.py), never a second repair.

REPAIR_SYSTEM_PROMPT below is genuinely fixed and never interpolated -
found live during the itinerary-quality/token-reduction pass that the
previous build_repair_prompt() concatenated the per-request FAILURES list
straight onto this text and sent the result as the SYSTEM message, despite
this module's own (then-inaccurate) docstring claiming it "never changes
between requests". That defeats provider-side prompt caching outright
(Groq/Gemini both cache on an identical system+tools prefix - see
console.groq.com/docs/prompt-caching and ai.google.dev/gemini-api/docs/caching)
on every single repair call, the one call site where the payload is small
enough to plausibly clear the cache floor. FAILURES now goes into a
HumanMessage instead (build_repair_failures_message(), sent by
_repair_node as its own message) - app/core/react.py's run_react() was
changed alongside this to keep the caller's original non-system messages
in the finalize call rather than discarding them, so this still reaches
the model at finalization too.
"""
from app.models.schemas import RepairedPlannerOutput
from app.prompts._base import PromptSpec

REPAIR_SYSTEM_PROMPT = """Your previous output failed validation. Fix ONLY the problems
listed in the FAILURES message below and return the corrected object. Change nothing
else - do not re-plan, re-rank, or restate parts of the output that passed validation.

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
    version="2.0.0",
    system=REPAIR_SYSTEM_PROMPT,
    output_schema=RepairedPlannerOutput,
)


def build_repair_failures_message(failures: list[str]) -> str:
    """The per-request part of the repair prompt, now a HumanMessage rather
    than concatenated onto the system prompt - see this module's docstring
    for why. Never a vague "invalid output"; always the precise failure list."""
    failure_lines = "\n".join(f"- {f}" for f in failures)
    return f"FAILURES\n{failure_lines}\n"


# The finalization-call variant (app/core/react.py's `finalize_system` param)
# - REPAIR_SYSTEM_PROMPT's "call the relevant tool again" line is exactly
# the kind of tool-mandating language that pulled Gemini/Groq toward
# attempting a phantom tool call at the toolless finalization step live
# (2026-09-02/03) - see app/core/react.py's docstring. This variant drops
# that line; if a repair loop genuinely called a tool to get a fresh
# number, that observation is already in the conversation to copy from.
# Also genuinely fixed text now, for the same caching reason as above -
# FAILURES reaches the finalize call via the HumanMessage from
# build_repair_failures_message(), which react.py's run_react() now
# preserves into the finalize call rather than discarding.
REPAIR_FINALIZE_SYSTEM = """Your previous output failed validation. Fix ONLY the problems listed
in the FAILURES message below and return the corrected object. Change nothing else - do not
re-plan, re-rank, or restate parts of the output that passed validation. No tools are available in
this message - never attempt to call one; none exist in this turn.

CONSTRAINTS
- Use only listing_ids that already appeared in this conversation's tool observations.
- Do not re-rank anything. Do not add or remove days beyond what was already there, unless a
  listed failure specifically requires it (e.g. day_count).
- Do not restate any cost or distance number yourself - use only values already present in a tool
  observation above, copied verbatim.
- Return only the corrected structured object. No prose outside it.
"""
