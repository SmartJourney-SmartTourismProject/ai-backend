"""
RAG evaluation - the evidence for whether the knowledge base actually
answers what it should and refuses what it shouldn't. Reads
tests/rag_golden.yaml, hits the real database, embedding API and chat LLM
(this is a live check, not a mocked unit test - the pytest suite already
covers routing/parsing/fallbacks with everything faked).

    python scripts/eval_rag.py                # retrieval + full answer pipeline
    python scripts/eval_rag.py --retrieval-only   # skip the LLM calls (fast, free)

Reports three numbers against the targets set when RAG was built
(2026-09-30):
    hit@5            >= 80%   - did the right document surface in retrieval at all
    refusal accuracy  = 100%  - every out-of-scope question was refused, none guessed
    citation coverage  = 0 uncited answers - every real answer names its source(s)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

GOLDEN_PATH = REPO_ROOT / "tests" / "rag_golden.yaml"
REFUSAL_TEXT = "I don't have reliable information on that."


@dataclass
class CaseResult:
    question: str
    answerable: bool
    hit_at_5: bool | None          # None when retrieval found nothing to rank at all
    top_titles: list[str]
    answered: bool | None = None   # None in --retrieval-only mode
    cited: bool | None = None


def load_golden(path: Path = GOLDEN_PATH) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


async def _check_retrieval(case: dict) -> CaseResult:
    from app.rag.retrieve import retrieve

    passages = await retrieve(case["question"], top_k=5)
    titles = [p.title for p in passages]
    hit = None
    if case["answerable"]:
        expected = case["expect_source_title"].lower()
        hit = any(expected in t.lower() for t in titles)
    return CaseResult(question=case["question"], answerable=case["answerable"], hit_at_5=hit, top_titles=titles)


async def _check_full_answer(case: dict, retrieval: CaseResult) -> CaseResult:
    from app.core.orchestrator import _answer_node
    from app.core.state import TripState

    state = TripState(user_input=case["question"], question=case["question"], intent="question")
    result = await _answer_node(state)

    refused = result.answer == REFUSAL_TEXT
    retrieval.answered = not refused
    retrieval.cited = bool(result.sources) if not refused else None
    return retrieval


async def run(retrieval_only: bool) -> int:
    cases = load_golden()
    results: list[CaseResult] = []

    for case in cases:
        retrieval = await _check_retrieval(case)
        if not retrieval_only:
            retrieval = await _check_full_answer(case, retrieval)
        results.append(retrieval)

        status = "?"
        if case["answerable"]:
            status = "hit " if retrieval.hit_at_5 else "MISS"
        print(f"  [{status}] {case['question']}")
        if case["answerable"] and not retrieval.hit_at_5:
            print(f"         expected {case['expect_source_title']!r}, got {retrieval.top_titles}")
        if not retrieval_only and not case["answerable"] and retrieval.answered:
            print("         should have refused, but answered")
        if not retrieval_only and case["answerable"] and retrieval.answered is False:
            print("         should have answered, but refused")

    answerable = [r for r in results if r.answerable]
    unanswerable = [r for r in results if not r.answerable]

    hit_rate = sum(1 for r in answerable if r.hit_at_5) / len(answerable) if answerable else 0.0
    print(f"\nRetrieval hit@5: {hit_rate:.0%} ({sum(1 for r in answerable if r.hit_at_5)}/{len(answerable)})")

    if not retrieval_only:
        refused_correctly = sum(1 for r in unanswerable if r.answered is False)
        refusal_rate = refused_correctly / len(unanswerable) if unanswerable else 0.0
        print(f"Refusal accuracy: {refusal_rate:.0%} ({refused_correctly}/{len(unanswerable)})")

        answered = [r for r in answerable if r.answered]
        uncited = [r for r in answered if not r.cited]
        print(f"Citation coverage: {len(answered) - len(uncited)}/{len(answered)} answered questions cited a source")
        if uncited:
            print(f"  Uncited: {[r.question for r in uncited]}")

        ok = hit_rate >= 0.80 and refusal_rate == 1.0 and not uncited
    else:
        ok = hit_rate >= 0.80

    print(f"\n{'PASS' if ok else 'FAIL'} against the 2026-09-30 targets.")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--retrieval-only", action="store_true", help="skip LLM answer calls (fast, free)")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args.retrieval_only)))
