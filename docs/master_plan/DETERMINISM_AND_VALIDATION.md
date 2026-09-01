# Determinism, Prompts, Validation, and the Scoring Engine

Companion to [`PROJECT_MASTER_PLAN.md`](PROJECT_MASTER_PLAN.md). This is the file to implement
against for concerns **#5, #6, #7, #10**. Everything here is exact on purpose — where a formula is
given, implement that formula, not an equivalent-looking one, or the determinism tests will drift.

---

## 1. The determinism ladder

Your own analysis (deterministic agents 1.1) is right, and this plan applies all five rungs. Where
each one lands in the codebase:

| Rung | Determinism | Where it lives |
|---|---|---|
| `temperature=0` | high | `app/config/settings.py` → `llm_temperature = 0.0`, plus explicit `top_p=1`, `top_k=1`, `candidate_count=1` |
| Strict, constrained prompts | higher | `app/prompts/` — §3 |
| Structured output | higher | `app/models/schemas.py` + `with_structured_output` — §4 |
| Fixed tool results | higher | tools read Postgres and caches; §5 L1 enforces the LLM can't alter them |
| **Deterministic Python ranking** | **very high** | `app/core/scoring.py`, `budget.py`, `itinerary.py` — §6, §7 |
| LLM makes the final decision | *less deterministic* | **eliminated.** The LLM's remaining outputs are: which tool to call, which items to *veto*, and free-text `reason` / `notes` / `theme` strings. |

**What we assert in tests** (§8): identical `listing_id` sets, identical ordering, identical
`estimated_cost`, identical day/item structure. **What we do not assert:** the exact wording of
`reason`, `notes`, `theme`, `final_response`. Asserting on prose would make the suite flaky for no
benefit — the prose carries no decisions.

---

## 2. LLM configuration

```python
# app/config/settings.py  (relevant fields)

# ✅ verified 2026-09-02: real model id, free-tier eligible (decision D6)
llm_model: str = "gemini-3.5-flash-lite"
llm_model_orchestrator: str = ""                 # blank → use llm_model
llm_provider_chain: str = "gemini:gemini-3.5-flash-lite,gemini:gemini-3.6-flash,groq:openai/gpt-oss-120b"
llm_temperature: float = 0.0
llm_top_p: float = 1.0
llm_top_k: int = 1
llm_timeout_s: float = 20.0
llm_max_retries: int = 1
groq_api_key: str = ""                           # optional; failover only
enable_response_narration: bool = False          # D6c — template by default
```

```python
# app/core/llm.py  — the ONLY place a chat model is constructed

_TOKEN_BUDGET = {
    "slots":        512,
    "orchestrator": 1024,
    "recommend":    2048,
    "plan":         3072,
    "respond":      512,
}

def _build(spec: str, purpose: str):
    """spec is "<provider>:<model>". Both clients take the same kwargs surface,
    so adding a provider is a branch here and nothing else."""
    provider, model = spec.split(":", 1)
    common = dict(temperature=settings.llm_temperature,
                  max_tokens=_TOKEN_BUDGET[purpose],
                  timeout=settings.llm_timeout_s,
                  max_retries=0)          # retries handled by the chain, not the client
    if provider == "gemini":
        return ChatGoogleGenerativeAI(model=model, google_api_key=settings.gemini_api_key,
                                      top_p=settings.llm_top_p, top_k=settings.llm_top_k, **common)
    if provider == "groq":
        return ChatGroq(model=model, api_key=settings.groq_api_key,
                        top_p=settings.llm_top_p, **common)
    raise ValueError(f"unknown LLM provider: {provider}")

@lru_cache(maxsize=16)
def get_llm(purpose: Literal["slots","orchestrator","recommend","plan","respond"]):
    """Returns the primary with the rest of the chain attached as fallbacks.
    LangChain's .with_fallbacks() preserves .bind_tools() and
    .with_structured_output(), so ReAct and structured output survive a switch."""
    specs = [s for s in settings.llm_provider_chain.split(",") if s.strip()]
    if purpose == "orchestrator" and settings.llm_model_orchestrator:
        specs = [f"gemini:{settings.llm_model_orchestrator}", *specs]
    usable = [s for s in specs if _has_key_for(s)]     # skip providers with no key configured
    primary, *rest = [_build(s, purpose) for s in usable]
    return primary.with_fallbacks(rest) if rest else primary
```

**Failover triggers on `429` (quota), `503`, and timeout — not on a validation failure.** A model
that returned a well-formed but wrong answer should go through the L3 repair path (§5), not to a
different provider; switching models there would just make the failure less reproducible.

Today `ChatGoogleGenerativeAI(...)` is constructed in three places with three different
configurations (and `RecommendationAgent`/`PlanningAgent` don't even pass the API key, relying on
`load_dotenv()` having populated `os.environ` — which works, but only because `main.py` calls it
first). One construction point removes that whole class of problem, and is what makes D6b a config
change rather than a rewrite.

### Call budget per trip plan (D6c)

| Call | Default | Notes |
|---|---|---|
| slot filling | always | small, ~300 tokens |
| orchestrator ReAct | 1–4 turns | bounded by `max_steps=6` |
| recommendation ReAct | 1–4 turns | **skipped entirely** on a shape-only follow-up |
| planner ReAct | 1–4 turns | |
| repair | only on validation failure | ≤1 |
| narration | **off by default** | `enable_response_narration=True` to turn on |

≈ **10–12 calls** per fresh plan, ≈ 4–6 per follow-up. Against ~1,500 RPD that's ~125 fresh plans
per day. Log the actual per-request count in Phase 8 and replace this estimate with the measurement.

Today `ChatGoogleGenerativeAI(...)` is constructed in three places with three different
configurations (and `RecommendationAgent`/`PlanningAgent` don't even pass the API key, relying on
`load_dotenv()` having populated `os.environ` — which works, but only because `main.py` calls it
first). One construction point removes that whole class of problem.

### Confirm the id and the live quota during Phase 0

`gemini-3.5-flash-lite` is verified as a real, free-tier model id (D6). Two things still need
checking against **your** key, because a wrong id fails at *request* time (mid-demo), not at import,
and Google no longer publishes free-tier ceilings:

1. Run `scripts/check_apis.py --llm` (it lists the models your key can actually call).
2. Read your real RPM/RPD at [aistudio.google.com/rate-limit](https://aistudio.google.com/rate-limit) and record it here.

The raw listing, if you want it directly:

```python
# scripts/list_models.py
import google.generativeai as genai
from app.config.settings import settings
genai.configure(api_key=settings.gemini_api_key)
for m in genai.list_models():
    if "generateContent" in m.supported_generation_methods:
        print(m.name, "| input:", m.input_token_limit, "| output:", m.output_token_limit)
```

Add a startup probe in `main.py`: one trivial `generateContent` call; on 404, log loudly and fall
back to `llm_model_fallback`. Better to know at boot than at request 1.

---

## 3. Prompt centralization

### Layout

```
app/prompts/
    __init__.py                    # the registry — the only public surface
    _base.py                       # PromptSpec, shared rule blocks
    slot_filling_prompt.py
    orchestrator_prompt.py
    recommendation_prompt.py
    planner_prompt.py
    repair_prompt.py
    response_prompt.py
```

`planning_prompt.py` and `recommendation_planning_prompt.py` are deleted — their content is split
correctly between `recommendation_prompt` (selection rules) and `planner_prompt` (itinerary rules),
which removes the current duplication where both files restate the same weather/budget/backtracking
rules.

```python
# app/prompts/_base.py
@dataclass(frozen=True)
class PromptSpec:
    name: str
    version: str                    # bump on every content change; logged with each call
    system: str
    output_schema: type[BaseModel]
    max_input_chars: int = 24_000   # guard against a runaway candidate payload

# app/prompts/__init__.py
PROMPTS: dict[str, PromptSpec] = {
    "slot_filling":   SLOT_FILLING_SPEC,
    "orchestrator":   ORCHESTRATOR_SPEC,
    "recommendation": RECOMMENDATION_SPEC,
    "planner":        PLANNER_SPEC,
    "repair":         REPAIR_SPEC,
    "response":       RESPONSE_SPEC,
}
def get_prompt(name: str) -> PromptSpec: ...
```

### The lint test that keeps it true

```python
# tests/test_prompts_centralized.py
def test_no_prompt_strings_outside_app_prompts():
    """A triple-quoted string containing an instruction verb, defined outside
    app/prompts/, is a prompt that escaped the registry."""
    offenders = scan(root="app", exclude="app/prompts",
                     pattern=r'"""(?s).{200,}?(You are|Your task|Rules:|Return strictly)')
    assert not offenders, f"Prompts must live in app/prompts/: {offenders}"
```

This is what stops #5 from silently regressing. Right now `app/utils/slot_filling.py` holds a
24-line `_SYSTEM_PROMPT` inline — that's exactly the case the test catches.

### Prompt style — the constrained form

Every agent prompt follows the same skeleton, which is your §1.1 point 2 made into a template:

```
ROLE       one sentence.
INPUT      what you will receive, field by field.
TOOLS      what each does, and when to call it.
RULES      numbered, imperative, testable. Include the MUST NOTs.
OUTPUT     "Return only the structured object. No prose outside it."
```

Worked example — `recommendation_prompt.py`:

```
You are the Recommendation Agent for a Sri Lanka travel assistant.

RULES
1.  Recommend ONLY items whose `listing_id` appeared in a db_search_* tool observation
    in this conversation. Never invent a place, and never recall one from memory.
2.  You MUST call score_candidates before producing an answer. The order it returns
    is final.
3.  You MUST NOT reorder, re-score, or re-weight its output. Copy `rank` and `score`
    verbatim.
4.  You MAY drop an item, and only for one of: closed_on_trip_dates,
    violates_must_avoid, duplicate_of, unsafe_area. When you drop item at rank N,
    take the next-ranked item in its place.
5.  Select at most: 3 hotels, 2 x duration_days restaurants, 3 x duration_days
    attractions, 5 events.
6.  `reason` explains why this item suits THIS traveller, in <= 25 words. It must not
    contain numbers you calculated yourself — quote score breakdown values only.
7.  If a category has fewer results than the maximum, return what exists and add a
    coverage_note. Never pad.
8.  Return only the RecommendationOutput object. No text outside it.
```

Note rule 6's clause: LLMs love to write "only 2 km away!" and be wrong. It may quote the
`breakdown` the scorer returned; it may not compute.

---

## 4. Structured output

`app/models/schemas.py` — one module, every LLM output model (the models are listed in
[`AGENT_ARCHITECTURE.md §3`](AGENT_ARCHITECTURE.md)). Every call site:

```python
llm = get_llm("recommend").bind_tools(tools)
structured = llm.with_structured_output(RecommendationOutput)   # never raw .ainvoke
```

**Delete both `_parse_json_response` methods.** They are the current silent-failure path: a model
response that isn't parseable JSON becomes `{}`, which becomes an empty itinerary, which becomes a
generic error with no indication that the model actually replied fine and the parser just gave up.

Constrain the schemas themselves, not only the prompt — the schema is enforced, the prompt is
merely read:

```python
class Selection(BaseModel):
    listing_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    rank: int = Field(ge=1, le=50)
    score: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=200)

class ItineraryDay(BaseModel):
    day: int = Field(ge=1, le=30)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    items: list[ItineraryItem] = Field(min_length=1, max_length=12)
```

Field-level constraints turn a whole class of L2 checks into free ones.

---

## 5. Output validation

### Layers

| Layer | Checks | Cost | On failure |
|---|---|---|---|
| **L0 — schema** | Pydantic model validity, types, patterns, bounds | ~0.1 ms | repair |
| **L1 — referential** | every `listing_id` exists in this request's candidate pool; every `lat`/`lon` equals the DB row's (±1e-6); `rank`/`score` match the `score_candidates` observation; no invented names | ~0.5 ms | repair |
| **L2 — business rules** | see below | ~1 ms | repair |
| **L3 — repair** | one LLM retry with a precise error list | 1 LLM call | → deterministic fallback |

Total happy-path cost **< 2 ms**, worst case < 5 ms. No LLM call unless something is actually
wrong. That satisfies #7's latency requirement.

### L2 rules

```python
RULES = [
  ("day_count",      lambda p, c: len(p.itinerary) == c.duration_days),
  ("dates_in_window",lambda p, c: all(d.date in c.date_window.dates for d in p.itinerary)),
  ("days_sequential",lambda p, c: [d.day for d in p.itinerary] == list(range(1, len(p.itinerary)+1))),
  ("no_duplicates",  lambda p, c: no_listing_twice_in_a_day(p)),
  ("times_ordered",  lambda p, c: all(times_strictly_increasing(d) for d in p.itinerary)),
  ("cost_consistent",lambda p, c: abs(p.estimated_cost - sum(d.day_cost for d in p.itinerary)) < 1.0),
  ("cost_recomputes",lambda p, c: abs(p.estimated_cost - estimate_costs(p).total) < 1.0),
  ("budget_honest",  lambda p, c: c.budget is None or p.estimated_cost <= c.budget or p.budget_notes),
  ("geo_in_country", lambda p, c: all(5.85 <= i.lat <= 9.95 and 79.5 <= i.lon <= 82.0 for i in items(p))),
  ("geo_near_dest",  lambda p, c: all(haversine(i, c) <= 150 for i in items(p))),
  ("weather_respect",lambda p, c: no_outdoor_items_on_days(p, c, rain_p_gte=0.6)),
  ("disaster_avoid", lambda p, c: no_items_within(p, c.disaster.red_zones, km=50)),
  ("must_avoid",     lambda p, c: not any(tag_hit(i, c.must_avoid) for i in items(p))),
  ("currency",       lambda p, c: p.currency == "LKR" and all(i.currency == "LKR" for i in items(p))),
]
```

`cost_recomputes` is the important one and the direct fix for the §12-case-1 failure: the plan's
claimed cost is **recomputed from the DB rows** and must match. An LLM cannot narrate its way past
an arithmetic check.

### The repair prompt

Precise, bounded, and it never gets to start over:

```
Your previous output failed validation. Fix ONLY these problems and return the
corrected object. Change nothing else.

FAILURES
- day_count: expected 3 days, got 2
- L1.listing_id: "9f2c-…" on day 2 item 3 was never returned by db_search_listings
- cost_recomputes: you reported 48000, recomputed value is 61250

CONSTRAINTS
- Use only listing_ids from the tool observations above.
- Do not re-rank. Do not add days beyond 3.
- Do not restate costs; call estimate_costs and copy the result.
```

One attempt. Then `fallback`.

### Validating the *inputs* too

`app/utils/validators.py` stays (it's correct) and gains: `budget` upper sanity bound
(LKR 100,000,000 → reject as a typo), `duration_days ≤ 30`, `travelers ≤ 20`, `user_input` length
≤ 2,000 chars, and a check that `trip_dates` are not in the past.

---

## 6. The scoring engine — `app/core/scoring.py`

This is concern **#10**, in full. **Pure functions. No I/O. No LLM. Fully unit-testable.**

### 6.1 The formula

```
score(item) = w_pref · pref(item) + w_prox · prox(item) + w_rate · rate(item) + w_cost · cost(item)
```

All four sub-scores are in `[0, 1]`. Weights sum to 1.0 per category.

### 6.2 Sub-scores, exactly

**Preference** — tag overlap between the traveller's interests and the listing's canonical tags
(`travel_listing.tags text[]`, populated at ingest by the OSM-tag → canonical-tag mapping in
[`DATA_PLATFORM.md §5.3`](DATA_PLATFORM.md)):

```python
def pref(item, interests) -> float:
    if not interests:
        return 0.5                                   # neutral: let other factors decide
    overlap = len(set(item.tags) & set(interests))
    return min(overlap / min(len(interests), 3), 1.0)
```

Dividing by `min(len(interests), 3)` means a traveller with 8 interests isn't punished for items
that can only match 2 of them — matching any 3 is a perfect preference fit.

**Proximity** — travel minutes from the day's anchor (start location on day 1, otherwise the hotel):

```python
D_MAX_MIN = 90

def prox(item, anchor, matrix) -> float:
    minutes = matrix.minutes(anchor, item) or haversine_minutes(anchor, item)
    return clamp(1.0 - minutes / D_MAX_MIN, 0.0, 1.0)

def haversine_minutes(a, b) -> float:
    straight = haversine_km(a, b)
    return (straight * 1.35) / _avg_kmh(straight) * 60.0

def _avg_kmh(straight_km: float) -> float:
    """Distance-banded, because one constant is wrong at both ends.

    Measured against ORS 2026-09-02, Colombo->Kandy: real 121 km / 128 min
    (~57 km/h). A flat 32 km/h predicted 239 min - nearly 2x over, which
    would push prox() toward 0 on every inter-city leg and make the scorer
    over-prefer whatever is nearest. The 1.35 road factor checked out fine
    (127.6 predicted vs 121 actual).

    Provisional. Calibrate in Phase 4 against ~20 real ORS pairs spanning
    flat and hill-country routes, then freeze these constants."""
    if straight_km < 5:    return 18.0    # urban, congested
    if straight_km < 30:   return 35.0    # suburban / secondary roads
    return 50.0                           # inter-city A-roads
```

Minutes, not kilometres — 20 km on the A1 and 20 km up to Ella are not the same trip, and the
whole point of proximity scoring is the traveller's time.

**Rating** — normalized, with Bayesian shrinkage so a single 5★ review can't beat a 4.4★ with 200:

```python
PRIOR_MEAN, PRIOR_WEIGHT = 0.5, 5.0

def rate(item) -> float:
    if item.rating is None:
        return 0.45                                  # slightly below neutral: unknown ≠ good
    r = clamp((item.rating - 1.0) / 4.0, 0.0, 1.0)   # 1–5 stars → 0–1
    n = item.rating_count or 0
    return (r * n + PRIOR_MEAN * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT)
```

**Cost** — this is the part you specifically asked for: *prefer medium/low even when budget allows.*

```python
LEVEL_BASE = {1: 0.90, 2: 1.00, 3: 0.65, 4: 0.35}    # note: level 2 scores highest, not level 1

def cost(item, budget_per_day_for_category) -> float:
    base = LEVEL_BASE.get(item.price_level, 0.60)     # unknown level → mild neutral
    if budget_per_day_for_category is None:
        return base
    est = estimated_item_cost(item)                   # exact price if known, else cost_reference
    ratio = est / budget_per_day_for_category
    fit = 1.0 if ratio <= 1.0 else 0.6 if ratio <= 1.5 else 0.2
    return 0.6 * base + 0.4 * fit
```

Level 2 (medium) beating level 1 (cheapest) is deliberate and is exactly your point 10: rock-bottom
options are usually worse experiences, so the *preferred* band is medium, with low close behind and
expensive penalized — independently of whether the budget could stretch.

### 6.3 Weights per category

| Category | `w_pref` | `w_prox` | `w_rate` | `w_cost` | Rationale |
|---|---|---|---|---|---|
| **attraction** | 0.45 | 0.25 | 0.20 | 0.10 | It's why they came. Most attractions are free/cheap, so cost matters least. |
| **restaurant** | 0.30 | 0.25 | 0.20 | 0.25 | Proximity matters (you eat where you are), cost recurs 2×/day. |
| **hotel** | 0.25 | 0.25 | 0.20 | 0.30 | Biggest single line item; location determines every day's travel. |
| **event** | 0.50 | 0.25 | 0.10 | 0.15 | Either it interests them or it's noise. Ratings are usually absent. |

Your baseline (0.5 / 0.3 / 0.2) is the attraction row, essentially — the per-category variants just
make room for the cost factor you asked to add. Weights live in one dict in `scoring.py`, so they're
tunable in one place, and every change must be re-run against the golden scenarios.

### 6.4 Hard filters — applied *before* scoring

Scoring ranks; it does not exclude. These exclude:

```python
def hard_filter(items, ctx) -> list[Item]:
    return [i for i in items if
        i.is_verified and i.is_active
        and not tag_hit(i, ctx.must_avoid)                    # "no hiking, my knees are bad"
        and not in_red_disaster_zone(i, ctx.disaster, km=50)
        and (i.price_level is None or i.price_level <= ctx.max_price_level)
        and open_during(i, ctx.date_window)                   # only when opening_hours known
    ]
```

### 6.5 Ranking and tie-breaks

```python
def rank(items, ctx, category) -> list[Ranked]:
    w = WEIGHTS[category]
    scored = []
    for i in hard_filter(items, ctx):
        b = Breakdown(pref=pref(i, ctx.interests), prox=prox(i, ctx.anchor, ctx.matrix),
                      rating=rate(i), cost=cost(i, ctx.budget_per_day[category]))
        s = round(w.pref*b.pref + w.prox*b.prox + w.rate*b.rating + w.cost*b.cost, 6)
        scored.append(Ranked(item=i, score=s, breakdown=b))

    scored.sort(key=lambda r: (-r.score, -(r.item.rating_count or 0), r.item.id))
    for n, r in enumerate(scored, start=1):
        r.rank = n
    return scored
```

Three details that matter more than they look:
- **`round(..., 6)` before sorting.** Float noise in the 15th decimal place otherwise reorders equal
  items between runs, and your determinism test fails for no real reason.
- **`r.item.id` as the final tie-break.** Total ordering, so equal scores are still deterministic.
- **`breakdown` is returned to the agent**, so `reason` can honestly say "highly rated and close to
  your hotel" without the model inventing figures.

### 6.6 What the agent receives

```json
{"ranked": [
  {"listing_id": "…", "rank": 1, "score": 0.8125, "name": "Temple of the Sacred Tooth Relic",
   "breakdown": {"pref": 1.0, "prox": 0.91, "rating": 0.86, "cost": 0.94},
   "cost_estimate": {"value": 2000, "currency": "LKR", "basis": "cost_reference"}}
]}
```

---

## 7. The budget engine — `app/core/budget.py`

### Allocation

```python
DEFAULT_SPLIT = {"stay": 0.40, "food": 0.25, "activity": 0.20, "transport": 0.15}
```

Adjusted by `travel_style`: `budget` → stay 0.32 / food 0.26 / activity 0.26 / transport 0.16;
`luxury` → stay 0.50 / food 0.25 / activity 0.15 / transport 0.10. One dict, documented.

```python
budget_per_day[category] = budget * split[category] / duration_days / (travelers if per_person else 1)
```

### Per-item cost

Precedence, and the `basis` string is returned so the UI can show confidence:

1. `travel_listing.price_per_night` (hotels, from Booking) → `basis: "exact"`
2. `local_event.price_min` → `basis: "exact"`
3. `cost_reference[district][category][price_level]` → `basis: "reference"`
4. National fallback in `cost_reference` where `district_id IS NULL` → `basis: "national"`
5. No data → excluded from the cost total, listed in `unknown_cost_items[]`, and mentioned in `budget_notes`

Never silently assume zero. A plan that looks affordable because three items had no price is worse
than one that says "3 items have no price data".

### Feasibility, before planning

```python
def feasibility(selections, ctx) -> Feasibility:
    cheapest = minimal_viable_plan_cost(selections, ctx)   # cheapest hotel + 2 meals/day + free attractions + transport
    return Feasibility(
        feasible = ctx.budget is None or cheapest <= ctx.budget,
        cheapest_total = cheapest,
        shortfall = max(0, cheapest - (ctx.budget or 0)),
    )
```

If infeasible, the planner is told **before** it builds anything, and `budget_notes` states the
minimum realistic cost. Today the system builds a LKR-1,400-equivalent plan on a $500 budget and
explains it afterwards; this reverses the order.

### `check_budget` → `cheapest_swaps`

When over budget, return concrete alternatives, cheapest-delta first:

```json
{"feasible": false, "total": 71200, "over_by": 11200,
 "cheapest_swaps": [
   {"replace": "hotel:abc", "with": "hotel:def", "saves": 9000, "score_delta": -0.06},
   {"replace": "restaurant:ghi", "with": "restaurant:jkl", "saves": 2400, "score_delta": -0.03}
 ]}
```

`score_delta` lets the agent take the *least damaging* saving rather than the first one, and it's
computed by the scorer, not guessed.

---

## 8. The determinism test protocol

`tests/test_determinism.py`, run in Phase 8 and in CI thereafter.

```python
@pytest.mark.parametrize("scenario", GOLDEN_SCENARIOS)   # the 12 from the master plan §6
async def test_identical_across_runs(scenario, seeded_db):
    runs = [await plan(scenario) for _ in range(3)]
    for r in runs[1:]:
        assert ids(r)        == ids(runs[0])           # selected listing_ids, in order
        assert structure(r)  == structure(runs[0])     # (day, time, type, listing_id) tuples
        assert r.estimated_cost == runs[0].estimated_cost
        # deliberately NOT asserted: reason / notes / theme / final_response
```

Plus a pure-unit determinism test with no network at all:

```python
def test_scoring_is_pure():
    a = rank(FIXTURE_ITEMS, FIXTURE_CTX, "attraction")
    b = rank(list(reversed(FIXTURE_ITEMS)), FIXTURE_CTX, "attraction")
    assert [x.item.id for x in a] == [x.item.id for x in b]   # input order must not matter
```

That second test is the one that catches an accidental unstable sort — the most common way a
"deterministic" ranker quietly stops being one.
