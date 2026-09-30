"""
The prompt registry - the only public surface for prompts in this codebase
(docs/master_plan/DETERMINISM_AND_VALIDATION.md §3, project concern #5).
Every prompt used anywhere in app/ is one of the specs below; a
`tests/test_prompts_centralized.py` lint test enforces that no other
triple-quoted instruction text lives outside this package.

Every entry here is live-wired: `slot_filling` by app/utils/slot_filling.py,
`recommendation`/`planner` by the two remaining app/agents/ ReAct agents,
`repair` by app/core/orchestrator.py's `_repair_node`, `answer` by its
`_answer_node` (RAG Q&A, app/rag/). There is no
`orchestrator` entry anymore - context resolution (destination/district/
date-window/weather/disaster) became deterministic
(app/core/context_resolver.py, "C2" in the itinerary-quality/token-
reduction pass) rather than an LLM call, so it has no prompt to register. A
`response` narration prompt existed here but was deleted
(AI_BACKEND_OPTIMIZATION_PLAN.md C3) - it was registered but never
consumed; `_respond_node` hardcodes its own template and
ENABLE_RESPONSE_NARRATION was never read anywhere. The pre-Phase-6 combined
prompt (recommendation_planning_prompt.py / planning_prompt.py) and the
workflows/ agents that used it were deleted in Phase 6, not kept alongside
their replacements - unlike earlier phases, here the replacement actually
exists.
"""
from app.prompts._base import PromptSpec
from app.prompts.slot_filling_prompt import SLOT_FILLING_SPEC
from app.prompts.recommendation_prompt import RECOMMENDATION_SPEC
from app.prompts.planner_prompt import PLANNER_SPEC
from app.prompts.repair_prompt import REPAIR_SPEC
from app.prompts.answer_prompt import ANSWER_SPEC

PROMPTS: dict[str, PromptSpec] = {
    "slot_filling": SLOT_FILLING_SPEC,
    "recommendation": RECOMMENDATION_SPEC,
    "planner": PLANNER_SPEC,
    "repair": REPAIR_SPEC,
    "answer": ANSWER_SPEC,
}


def get_prompt(name: str) -> PromptSpec:
    try:
        return PROMPTS[name]
    except KeyError:
        raise KeyError(f"Unknown prompt '{name}'. Known: {', '.join(PROMPTS)}") from None
