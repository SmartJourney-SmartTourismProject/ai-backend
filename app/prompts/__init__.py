"""
The prompt registry - the only public surface for prompts in this codebase
(docs/master_plan/DETERMINISM_AND_VALIDATION.md §3, project concern #5).
Every prompt used anywhere in app/ is one of the specs below; a
`tests/test_prompts_centralized.py` lint test enforces that no other
triple-quoted instruction text lives outside this package.

Every entry here is live-wired as of Phase 6 (PROJECT_MASTER_PLAN.md):
`slot_filling` by app/utils/slot_filling.py, `orchestrator`/`recommendation`/
`planner` by the three app/agents/ ReAct agents, `repair` by
app/core/orchestrator.py's `_repair_node`. `response` stays unused until
ENABLE_RESPONSE_NARRATION is turned on (decision D6c) - the template in
app/prompts/response_prompt.py's `template_final_response()` is the default
path. The pre-Phase-6 combined prompt (recommendation_planning_prompt.py /
planning_prompt.py) and the workflows/ agents that used it were deleted in
Phase 6, not kept alongside their replacements - unlike earlier phases,
here the replacement actually exists.
"""
from app.prompts._base import PromptSpec
from app.prompts.slot_filling_prompt import SLOT_FILLING_SPEC
from app.prompts.orchestrator_prompt import ORCHESTRATOR_SPEC
from app.prompts.recommendation_prompt import RECOMMENDATION_SPEC
from app.prompts.planner_prompt import PLANNER_SPEC
from app.prompts.repair_prompt import REPAIR_SPEC
from app.prompts.response_prompt import RESPONSE_SPEC

PROMPTS: dict[str, PromptSpec] = {
    "slot_filling": SLOT_FILLING_SPEC,
    "orchestrator": ORCHESTRATOR_SPEC,
    "recommendation": RECOMMENDATION_SPEC,
    "planner": PLANNER_SPEC,
    "repair": REPAIR_SPEC,
    "response": RESPONSE_SPEC,
}


def get_prompt(name: str) -> PromptSpec:
    try:
        return PROMPTS[name]
    except KeyError:
        raise KeyError(f"Unknown prompt '{name}'. Known: {', '.join(PROMPTS)}") from None
