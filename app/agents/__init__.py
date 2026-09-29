"""
The Phase 6 ReAct agents (docs/master_plan/AGENT_ARCHITECTURE.md §3) -
replaces app/workflows/, which held single-shot, hand-parsed LLM calls.
Each agent subclasses app/core/base_agent.py's BaseAgent and drives
app/core/react.py's run_react() with its own tool subset
(app/tools/registry.py) and prompt (app/prompts/).
"""
