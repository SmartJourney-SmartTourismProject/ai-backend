# tests/test_prompts_centralized.py
# Project concern #5: every prompt lives in app/prompts/. This test is what
# keeps that true after today - a triple-quoted string that reads like an
# instruction to an LLM, defined anywhere else in app/, is a prompt that
# escaped the registry. Exactly the case app/utils/slot_filling.py's old
# inline _SYSTEM_PROMPT was, before Phase 5 moved it into
# app/prompts/slot_filling_prompt.py.
import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "app"
PROMPTS_DIR = APP_ROOT / "prompts"

# A triple-quoted string of at least 200 chars containing one of these
# phrases is almost certainly an LLM instruction, not an ordinary docstring -
# ordinary docstrings don't say "Return strictly" or "You are the X Agent".
#
# Deliberately narrow: bare "RULES" (no colon) was tried first and produced
# a false positive on app/core/output_validator.py, whose own docstring
# legitimately says "the RULES list below" (RULES is a Python variable name
# there, not an instruction to a model) - "Rules:" with a colon is what the
# actual prompt template (docs/master_plan/DETERMINISM_AND_VALIDATION.md §3)
# uses and doesn't collide with ordinary prose about a rules list.
_PROMPT_PATTERN = re.compile(r'"""(?P<body>.{200,}?)"""', re.DOTALL)
_INSTRUCTION_MARKERS = ("You are", "Your task", "Rules:", "RULES\n", "Return strictly", "Return only the")


def _scan_for_prompt_strings(root: Path, exclude: Path) -> list[str]:
    offenders = []
    for path in root.rglob("*.py"):
        if exclude in path.parents or path.parent == exclude:
            continue
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _PROMPT_PATTERN.finditer(text):
            body = match.group("body")
            if any(marker in body for marker in _INSTRUCTION_MARKERS):
                offenders.append(str(path.relative_to(root.parent)))
    return offenders


def test_no_prompt_strings_outside_app_prompts():
    offenders = _scan_for_prompt_strings(APP_ROOT, PROMPTS_DIR)
    assert not offenders, (
        f"Prompt-shaped strings found outside app/prompts/: {offenders}. "
        f"Move them into a PromptSpec in app/prompts/ and register it in app/prompts/__init__.py."
    )


def test_every_registered_prompt_has_the_required_fields():
    from app.prompts import PROMPTS

    assert PROMPTS, "the prompt registry is empty"
    for name, spec in PROMPTS.items():
        assert spec.name == name, f"{name}: PromptSpec.name mismatch ({spec.name!r})"
        assert spec.version, f"{name}: version must be set"
        assert spec.system.strip(), f"{name}: system prompt text is empty"
        assert spec.max_input_chars > 0, f"{name}: max_input_chars must be positive"


def test_slot_filling_prompt_matches_its_live_schema():
    """The one prompt that's actually wired into a running agent today -
    confirms the registry entry app/utils/slot_filling.py reads from is the
    real one, not a stale duplicate."""
    from app.prompts import get_prompt
    from app.models.schemas import ExtractedSlots

    spec = get_prompt("slot_filling")
    assert spec.output_schema is ExtractedSlots


def test_get_prompt_unknown_name_raises_clearly():
    from app.prompts import get_prompt

    import pytest
    with pytest.raises(KeyError, match="Unknown prompt"):
        get_prompt("not_a_real_prompt")
