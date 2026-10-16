"""
Multi-agent cost-optimized answering with critic/evaluator consensus.

A LangGraph workflow: a fast router picks a tier, that tier's specialist
answers, a critic from another model family lists problems, an evaluator grades
the answer against RUBRIC, and only an answer that fails is escalated to a
frontier model.

    from flow import multi_agent
    state = multi_agent.run('Explain board-and-train pricing')
    state['final_answer']

    multi_agent.complete_structured(prompt, ResearchOutput, system=...)

The pipeline agents reach this through flow.llm.complete_structured: set
LLM_MODEL, or an agent's own RESEARCH_MODEL / DRAFTING_MODEL / ANALYSIS_MODEL,
to flow.llm.MULTI_AGENT_MODEL ('multi-agent').

Transport is flow.llm.chat_completion -- the same httpx path, proxy-key header
and deadline as every other model call. The proxy is LITELLM_PROXY_URL /
LITELLM_PROXY_API_KEY, falling back to LITELLM_BASE_URL / LITELLM_API_KEY: the
ollama/ and openrouter/ model groups below may live on a different proxy from
the one LLM_MODEL is served by.

Environment, all read per call:
  LITELLM_CONFIG            proxy config.yaml; its model_list filters
                            ROLE_PREFERENCES. Absent: the lists are used as-is.
  AGENT_REQUEST_TIMEOUT     seconds for one router call (default 90)
  CHECK_REQUEST_TIMEOUT     seconds for one critic or evaluator call (default 180)
  ANSWER_REQUEST_TIMEOUT    seconds for one answering call -- specialist,
                            frontier, repair (default 300; nemotron-3-ultra
                            regularly ran past 90 on analysis prompts)
  MULTI_AGENT_CALL_TIMEOUT  seconds for a whole run, every node and repair
                            included (default 1800)

Per agent, via flow.llm.agent_setting (RESEARCH_TIER, RESEARCH_REASONING_EFFORT):
  tier              skip the router and answer with this tier's models
  reasoning_effort  low/medium/high on the answering calls only -- specialist,
                    frontier, repair. 'high' is the strongest that works here:
                    Ollama also accepts 'max', but on nemotron-3-ultra and
                    gemma4 it switched thinking off entirely (0 reasoning
                    tokens), and on gpt-oss it reasoned less than 'high'.
  MULTI_AGENT_MLFLOW        truthy: log each run to MLflow
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, Type, TypedDict

import yaml
from pydantic import BaseModel, ValidationError

from flow.llm import (
    PROXY_URL,
    LLMSchemaError,
    LLMTransportError,
    chat_completion,
    json_schema_format,
)

# ======================
# STATE DEFINITION
# ======================

Tier = Literal["cheap", "mid", "code", "frontier"]
TIERS = ("cheap", "mid", "code", "frontier")


class AgentState(TypedDict):
    """State that flows through the LangGraph workflow"""
    user_prompt: str  # what the specialist is sent: the request plus any schema text
    original_prompt: str  # the request as the caller wrote it; what "on point" means
    system: Optional[str]
    response_format: Optional[dict]
    deadline: Optional[float]  # time.monotonic() value; None means no limit
    forced_tier: Optional[Tier]  # set: the router is skipped
    reasoning_effort: Optional[str]  # set: sent on the answering calls
    route_decision: Tier
    route_method: str
    specialist_output: str
    specialist_reasoning: str
    criticisms: List[str]
    critic_confidence: float
    consensus_agreement: bool
    rubric_scores: Dict[str, int]
    rubric_score: float
    evaluator_feedback: str
    evaluation_method: str
    unmet_requirements: List[str]
    final_answer: str
    answer_source: str  # "specialist" or "frontier": which answer final_answer is
    final_on_point: Optional[bool]  # None: no evaluator could check it
    escalated_to_frontier: bool
    frontier_output: str
    frontier_error: str
    frontier_rubric_scores: Dict[str, int]
    frontier_rubric_score: float
    frontier_unmet_requirements: List[str]
    frontier_evaluation_method: str
    models_used: Dict[str, str]
    total_cost: float
    latency_ms: int
    timestamp: str

# ======================
# CONFIGURATION
# ======================

REPO_ROOT = Path(__file__).resolve().parent.parent

# A 402/403 means the plan doesn't include the model. That holds for a run, but
# a long-lived serve process outlives plan changes, so it expires.
UNAVAILABLE_TTL = 3600.0
_unavailable: Dict[str, float] = {}  # model group -> time.monotonic() marked
_role_models: Optional[Dict[str, List[str]]] = None
_graph = None

# Ranked model groups per role, limited to models that answer on free plans.
# Names not present in config.yaml are dropped at load time, so this list can go stale safely.
# Excluded (Ollama free plan returns 402/403): deepseek-*, glm-*, kimi-*, minimax-*, mistral-large-*.
# Excluded (OpenRouter 403, "agentic harnesses only"): thinkingmachines/inkling*.
ROLE_PREFERENCES: Dict[str, List[str]] = {
    # Non-reasoning, fast models: the router only needs one word back.
    "router": [
        "ollama/gemma4:31b",
        "openrouter/nvidia/nemotron-3.5-lightning:free",
        "ollama/gpt-oss:20b",
    ],
    "cheap": [
        "ollama/gpt-oss:20b",
        "ollama/nemotron-3-nano:30b",
        "ollama/gemma4:31b",
        "openrouter/nvidia/nemotron-3.5-lightning:free",
        "openrouter/dots-studio/dots-3-note-preview:free",
        "openrouter/google/gemma-4-31b-it:free",
    ],
    "mid": [
        "ollama/gpt-oss:120b",
        "ollama/nemotron-3-super",
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
        "ollama/gemma4:31b",
        "openrouter/dots-studio/dots-3-note-preview:free",
    ],
    "code": [
        "ollama/gpt-oss:120b",
        "openrouter/poolside/laguna-s-2.1:free",
        "openrouter/cohere/north-mini-code:free",
        "ollama/nemotron-3-super",
        "ollama/gpt-oss:20b",
    ],
    "frontier": [
        "ollama/nemotron-3-ultra",
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        "ollama/gpt-oss:120b",
        "ollama/nemotron-3-super",
    ],
    "critic": [
        "ollama/gpt-oss:120b",
        "ollama/nemotron-3-super",
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
        "ollama/gemma4:31b",
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        "openrouter/dots-studio/dots-3-note-preview:free",
    ],
    # Strong graders; reordered at call time to avoid the specialist's and critic's families.
    "evaluator": [
        "ollama/gpt-oss:120b",
        "ollama/nemotron-3-ultra",
        "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        "ollama/nemotron-3-super",
        "ollama/gemma4:31b",
    ],
}

# Evaluator rubric. Each criterion is scored 1-5; the answer passes when the weighted mean
# reaches PASS_THRESHOLD and no criterion falls below its "min".
RUBRIC: Dict[str, Dict] = {
    "correctness": {
        "weight": 0.35, "min": 3,
        "description": "Facts, logic, code and calculations are accurate; no errors that would mislead the user.",
    },
    "completeness": {
        "weight": 0.20,
        "description": "Addresses every part of the request, including important edge cases and caveats.",
    },
    "relevance": {
        "weight": 0.15,
        "description": "Answers what was actually asked and follows any explicit instructions or constraints.",
    },
    "clarity": {
        "weight": 0.15,
        "description": "Well organized and easy to follow; concise without omitting needed detail.",
    },
    "groundedness": {
        "weight": 0.15,
        "description": "Claims are supported or appropriately hedged; no fabricated sources, figures or risky advice.",
    },
}
PASS_THRESHOLD = 3.5


def proxy_url() -> str:
    return (os.environ.get("LITELLM_PROXY_URL")
            or os.environ.get("LITELLM_BASE_URL") or PROXY_URL)


def proxy_key() -> Optional[str]:
    return os.environ.get("LITELLM_PROXY_API_KEY") or os.environ.get("LITELLM_API_KEY")


def request_timeout() -> float:
    """Per-call limit for the router."""
    return float(os.environ.get("AGENT_REQUEST_TIMEOUT", "90"))


def check_request_timeout() -> float:
    """Per-call limit for the critic and evaluator, which read the request and the answer."""
    return float(os.environ.get("CHECK_REQUEST_TIMEOUT", "180"))


def answer_request_timeout() -> float:
    """Per-call limit for the answering calls: specialist, frontier, repair."""
    return float(os.environ.get("ANSWER_REQUEST_TIMEOUT", "300"))


def run_timeout() -> float:
    return float(os.environ.get("MULTI_AGENT_CALL_TIMEOUT", "1800"))


# Thinking counts against max_tokens. At 4000 a high-effort answer to a long
# research prompt can spend the whole budget thinking and come back empty.
REASONING_MAX_TOKENS = 16000
# The critic and evaluator models reason before replying even without an
# effort set. At 1500, gpt-oss:120b and nemotron-3-super came back empty on
# research prompts, and the evaluator's reply now also lists requirements.
CHECK_MAX_TOKENS = 4000


def answering_params(state: AgentState, max_tokens: int) -> Dict[str, Any]:
    """max_tokens and the answering timeout, plus reasoning_effort when set."""
    params: Dict[str, Any] = {"max_tokens": max_tokens,
                              "call_timeout": answer_request_timeout(),
                              "call_timeout_name": "ANSWER_REQUEST_TIMEOUT"}
    effort = state.get("reasoning_effort")
    if effort:
        params.update(max_tokens=max(max_tokens, REASONING_MAX_TOKENS), reasoning_effort=effort)
    return params


def checking_params() -> Dict[str, Any]:
    return {"max_tokens": CHECK_MAX_TOKENS,
            "call_timeout": check_request_timeout(),
            "call_timeout_name": "CHECK_REQUEST_TIMEOUT"}


def config_path() -> Path:
    return Path(os.environ.get("LITELLM_CONFIG") or REPO_ROOT / "config.yaml")


def reset_caches() -> None:
    """Forget the loaded role models and the models marked unavailable."""
    global _role_models
    _role_models = None
    _unavailable.clear()


def load_role_models(path: Optional[Path] = None) -> Dict[str, List[str]]:
    """Intersect ROLE_PREFERENCES with the model groups defined in config.yaml.

    Without a config there is nothing to intersect with, so the preference
    lists are used whole and chat()'s fallback chain skips what the proxy lacks.
    """
    path = path or config_path()
    if not path.exists():
        return {role: list(models) for role, models in ROLE_PREFERENCES.items()}

    with open(path) as f:
        config = yaml.safe_load(f) or {}
    available = list(dict.fromkeys(m["model_name"] for m in config.get("model_list", [])))
    if not available:
        raise ValueError(f"No models found in {path}")

    roles = {}
    for role, preferred in ROLE_PREFERENCES.items():
        models = [m for m in preferred if m in available]
        if not models:
            print(f"warning: no preferred '{role}' models in config; using all models", file=sys.stderr)
            models = available
        roles[role] = models
    return roles


def role_models() -> Dict[str, List[str]]:
    """load_role_models(), loaded on first use rather than at import."""
    global _role_models
    if _role_models is None:
        _role_models = load_role_models()
    return _role_models


def model_family(model_name: str) -> str:
    """'ollama/kimi-k2.6' -> 'kimi', 'openrouter/nvidia/nemotron-3-super...' -> 'nemotron'."""
    bare = model_name.split("/")[-1]
    return re.split(r"[-:.]", bare, maxsplit=1)[0].lower()


def prefer_other_families(models: Sequence[str], avoid: Sequence[str]) -> List[str]:
    """Reorder so models from a different family than `avoid` come first; drop exact repeats."""
    avoid_families = {model_family(m) for m in avoid}
    remaining = [m for m in models if m not in avoid] or list(models)
    return sorted(remaining, key=lambda m: model_family(m) in avoid_families)


def _is_unavailable(model: str) -> bool:
    marked = _unavailable.get(model)
    if marked is None:
        return False
    if time.monotonic() - marked > UNAVAILABLE_TTL:
        # pop, not del: concurrent workers can expire the same entry at once.
        _unavailable.pop(model, None)
        return False
    return True


def chat(
    models: Sequence[str],
    prompt: str,
    system: Optional[str] = None,
    response_format: Optional[dict] = None,
    deadline: Optional[float] = None,
    prior: Sequence[dict] = (),
    call_timeout: Optional[float] = None,
    call_timeout_name: Optional[str] = None,
    **params: Any,
) -> Tuple[str, str, float]:
    """Call the proxy with each model group in order until one returns content.

    `prior` are messages placed between the system prompt and `prompt`.
    `deadline` is a time.monotonic() value no call may run past;
    `call_timeout` (named `call_timeout_name` in errors) overrides
    AGENT_REQUEST_TIMEOUT for each attempt.
    Returns (content, model_used, cost). Raises LLMTransportError.
    """
    messages = [{"role": "system", "content": system}] if system else []
    messages += [*prior, {"role": "user", "content": prompt}]

    errors = []
    candidates = [m for m in models if not _is_unavailable(m)] or list(models)
    for model in candidates:
        limit = call_timeout or request_timeout()
        limit_name = (call_timeout_name or "call_timeout") if call_timeout else "AGENT_REQUEST_TIMEOUT"
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMTransportError(
                    f"MULTI_AGENT_CALL_TIMEOUT={run_timeout():g}s spent before "
                    f"trying {model!r}" + (f" -> {' || '.join(errors)}" if errors else ""))
            if remaining < limit:
                limit, limit_name = remaining, "what was left of MULTI_AGENT_CALL_TIMEOUT"
        try:
            content, cost = chat_completion(
                messages, model, response_format=response_format,
                base_url=proxy_url(), api_key=proxy_key(), deadline=limit,
                # Not retried in place: the next model is the retry, and
                # flow.llm's backoff would outlast MULTI_AGENT_CALL_TIMEOUT.
                timeout_name=limit_name, retry=False, **params,
            )
            if not content or not content.strip():
                raise ValueError("empty response")
            return content, model, cost
        except (LLMTransportError, ValueError, KeyError, IndexError, TypeError) as e:
            # 402/403 mean the plan doesn't include this model; that won't change mid-run.
            # 401/429 can be one bad or throttled credential in the group, so retry those next call.
            if getattr(e, "status_code", None) in (402, 403):
                _unavailable[model] = time.monotonic()
            errors.append(f"{model}: {type(e).__name__}: {str(e)[:200]}")
            print(f"multi-agent: {model} failed, trying next: {type(e).__name__}: {str(e)[:300]}")
    raise LLMTransportError("all models failed -> " + " || ".join(errors))


def elapsed_ms(start: datetime) -> int:
    return int((datetime.now() - start).total_seconds() * 1000)


def _instructions_block(state: AgentState) -> str:
    """The system prompt, shown to the critic and evaluator so they hold the answer to it."""
    if not state.get("system"):
        return ""
    return f"\nInstructions the answer had to follow:\n{state['system']}\n"


def _format_note(state: AgentState) -> str:
    if not state.get("response_format"):
        return ""
    return ("\nThe answer is required to be a JSON object matching a schema; judge its "
            "content, not the fact that it is JSON.\n")

# ======================
# NODE IMPLEMENTATIONS
# ======================

def keyword_route(prompt: str) -> Tier:
    """Fallback classifier used when the router model is unavailable."""
    prompt_lower = prompt.lower()
    cheap_keywords = ['typo', 'format', 'spell', 'grammar', 'define', 'what is', 'who is', 'when is']
    mid_keywords = ['explain', 'summarize', 'write', 'create', 'design']
    code_keywords = ['code', 'function', 'algorithm', 'python', 'javascript', 'sql', 'regex', 'bug', 'implement']
    frontier_keywords = ['research', 'analyze', 'compare', 'evaluate', 'optimize', 'prove', 'theorem', 'complex']

    scores = {
        "frontier": sum(kw in prompt_lower for kw in frontier_keywords),
        "code": sum(kw in prompt_lower for kw in code_keywords),
        "mid": sum(kw in prompt_lower for kw in mid_keywords),
        "cheap": sum(kw in prompt_lower for kw in cheap_keywords),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "cheap"


def route_node(state: AgentState) -> AgentState:
    """Classify the prompt into a tier with a fast model; fall back to keywords."""
    start_time = datetime.now()
    prompt = state["user_prompt"]

    router_prompt = f"""Classify the user request into exactly one tier. Reply with only the tier word.

cheap    - short factual lookups, definitions, spelling/grammar/formatting fixes
mid      - explanations, summaries, general writing, moderate reasoning
code     - writing, reviewing, or debugging source code, SQL, regex, or algorithms
frontier - deep analysis, research, multi-step reasoning, proofs, comparisons with trade-offs

User request:
{prompt}"""

    models_used = dict(state.get("models_used") or {})
    if state.get("forced_tier"):
        state.update({
            "route_decision": state["forced_tier"],
            "route_method": "forced",
            "models_used": models_used,
            "timestamp": datetime.now().isoformat(),
        })
        return state

    route_method = "keyword"
    route_decision = keyword_route(prompt)
    cost = 0.0
    try:
        answer, model, cost = chat(role_models()["router"], router_prompt,
                                   deadline=state.get("deadline"), temperature=0, max_tokens=10)
        match = re.search(r"\b(cheap|mid|code|frontier)\b", answer.lower())
        if match:
            route_decision = match.group(1)
            route_method = "llm"
            models_used["router"] = model
    except LLMTransportError as e:
        print(f"router model unavailable, using keyword routing: {e}", file=sys.stderr)

    state.update({
        "route_decision": route_decision,
        "route_method": route_method,
        "models_used": models_used,
        "total_cost": state.get("total_cost", 0.0) + cost,
        "latency_ms": state.get("latency_ms", 0) + elapsed_ms(start_time),
        "timestamp": datetime.now().isoformat()
    })
    return state


def specialist_node(state: AgentState) -> AgentState:
    """Run the selected specialist tier. Its failure fails the run: there is nothing to grade."""
    start_time = datetime.now()
    route_decision = state["route_decision"]
    models_used = dict(state.get("models_used") or {})

    specialist_output, model, cost = chat(
        role_models()[route_decision], state["user_prompt"],
        system=state.get("system"), response_format=state.get("response_format"),
        deadline=state.get("deadline"), temperature=0.2,
        **answering_params(state, 4000 if state.get("response_format") else 2000),
    )
    models_used["specialist"] = model

    state.update({
        "specialist_output": specialist_output,
        "specialist_reasoning": f"Generated by {route_decision} tier model ({model})",
        "models_used": models_used,
        "total_cost": state.get("total_cost", 0.0) + cost,
        "latency_ms": state.get("latency_ms", 0) + elapsed_ms(start_time)
    })
    return state


def critic_node(state: AgentState) -> AgentState:
    """Run critic agent to identify issues in specialist output."""
    start_time = datetime.now()
    models_used = dict(state.get("models_used") or {})

    critic_prompt = f"""You are a critical evaluator. Your job is to identify:
    1. Logical errors or inconsistencies
    2. Factual inaccuracies
    3. Missing information or edge cases
    4. Risky or unsubstantiated claims
    5. Departures from the instructions the answer had to follow

    Do NOT propose corrections. Only identify problems with severity scores.
    If there are no problems, reply with exactly: NO ISSUES

    Format output as:
    CRITICISM 1: [issue] | SEVERITY: HIGH/MED/LOW
    CRITICISM 2: [issue] | SEVERITY: HIGH/MED/LOW
    ...
    {_instructions_block(state)}{_format_note(state)}
    User prompt: {state["user_prompt"]}

    Specialist output to critique:
    {state["specialist_output"]}"""

    # A different model family than the specialist gives a more independent critique.
    critics = prefer_other_families(role_models()["critic"], [models_used.get("specialist", "")])
    cost = 0.0
    try:
        criticisms_text, model, cost = chat(critics, critic_prompt, deadline=state.get("deadline"),
                                            temperature=0.1, **checking_params())
        models_used["critic"] = model
        criticisms = [line.strip() for line in criticisms_text.split('\n') if line.strip() and '|' in line]
        high_severity_count = sum(1 for c in criticisms if 'SEVERITY: HIGH' in c.upper())
        critic_confidence = max(0.1, 1.0 - (high_severity_count * 0.3))
    except LLMTransportError as e:
        criticisms = [f"Error in critic agent: {e} | SEVERITY: HIGH"]
        critic_confidence = 0.1

    state.update({
        "criticisms": criticisms,
        "critic_confidence": critic_confidence,
        "models_used": models_used,
        "total_cost": state.get("total_cost", 0.0) + cost,
        "latency_ms": state.get("latency_ms", 0) + elapsed_ms(start_time)
    })
    return state


def parse_evaluation(text: str) -> Tuple[Dict[str, int], List[str], str]:
    """Extract {"scores": {...}, "unmet_requirements": [...], "feedback": "..."}
    from a model reply that may wrap it in prose. A missing unmet_requirements
    means none were listed."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in evaluator reply")
    data = json.loads(match.group(0))
    raw = data.get("scores", data)
    scores = {}
    for criterion in RUBRIC:
        if criterion not in raw:
            raise ValueError(f"evaluator omitted '{criterion}'")
        scores[criterion] = min(5, max(1, int(round(float(raw[criterion])))))
    unmet = data.get("unmet_requirements") or []
    if not isinstance(unmet, list):
        unmet = [unmet]
    unmet = [str(u).strip() for u in unmet if str(u).strip()]
    return scores, unmet, str(data.get("feedback", "")).strip()


def grade_answer(state: AgentState, answer: str, avoid: Sequence[str],
                 critic_note: str = "") -> Dict[str, Any]:
    """Hold `answer` to the user's original request and grade it against RUBRIC.

    The answer passes only with a weighted score of PASS_THRESHOLD, no
    criterion below its "min", and no unmet requirement: a polished answer to
    a different question fails. The grader is chosen from model families other
    than `avoid`. When no grade can be had, method is "critic-severity" and
    passed / on_point are None -- unchecked, not on point.
    """
    rubric_text = "\n".join(
        f"- {name} (weight {spec['weight']:.0%}"
        + (f", must score at least {spec['min']}" if "min" in spec else "")
        + f"): {spec['description']}"
        for name, spec in RUBRIC.items()
    )
    example = json.dumps({"scores": {name: 4 for name in RUBRIC},
                          "unmet_requirements": [], "feedback": "..."})
    evaluator_prompt = f"""You are the evaluator. Your job is to make sure the answer is on point with what the user asked for, then to grade it against the rubric below.

First work out every requirement the user's original request{" and the instructions" if state.get("system") else ""} place on the answer: what it must address, include, follow or avoid. Check the answer against each one. Every requirement it misses, ignores or contradicts goes in unmet_requirements, stated briefly; leave the list empty only when all are met. An answer that is polished but off point fails.

Use the critic's findings as evidence, but verify them yourself: a criticism can be wrong or overstated.
Score each criterion as an integer from 1 to 5 (1 = poor, 3 = acceptable, 5 = excellent).

Rubric:
{rubric_text}
{_instructions_block(state)}{_format_note(state)}
User's original request:
{state["original_prompt"]}

Answer to grade:
{answer}

Critic findings{critic_note}:
{chr(10).join(state["criticisms"]) or "(none)"}

Reply with only a JSON object in this shape, where feedback names the main weaknesses in one or two sentences:
{example}"""

    evaluators = prefer_other_families(role_models()["evaluator"], avoid)
    model, cost = None, 0.0
    try:
        reply, model, cost = chat(evaluators, evaluator_prompt, deadline=state.get("deadline"),
                                  temperature=0, **checking_params())
        scores, unmet, feedback = parse_evaluation(reply)
    except (LLMTransportError, ValueError) as e:
        print(f"multi-agent: evaluator unavailable ({type(e).__name__}: {str(e)[:200]}); "
              f"answer is unchecked")
        return {"scores": {}, "score": 0.0, "unmet": [], "method": "critic-severity",
                "feedback": f"Evaluator unavailable ({type(e).__name__}); decided on critic severity.",
                "passed": None, "on_point": None, "model": None, "cost": cost}

    total_weight = sum(spec["weight"] for spec in RUBRIC.values())
    score = sum(scores[c] * spec["weight"] for c, spec in RUBRIC.items()) / total_weight
    below_min = [c for c, spec in RUBRIC.items() if scores[c] < spec.get("min", 1)]
    if below_min:
        feedback = f"Below minimum on: {', '.join(below_min)}. {feedback}".strip()
    if unmet:
        feedback = f"Off point: {'; '.join(unmet)}. {feedback}".strip()
    return {"scores": scores, "score": score, "unmet": unmet, "method": "rubric",
            "feedback": feedback, "passed": score >= PASS_THRESHOLD and not below_min and not unmet,
            "on_point": not unmet, "model": model, "cost": cost}


def evaluate_node(state: AgentState) -> AgentState:
    """Hold the specialist's answer to the request; a failing answer escalates."""
    start_time = datetime.now()
    models_used = dict(state.get("models_used") or {})

    grade = grade_answer(state, state["specialist_output"],
                         [models_used.get("specialist", ""), models_used.get("critic", "")])
    if grade["method"] == "rubric":
        models_used["evaluator"] = grade["model"]
        passed = grade["passed"]
    else:
        # No usable grade: fall back to the critic's severity counts.
        passed = not any("SEVERITY: HIGH" in c.upper() for c in state["criticisms"])

    state.update({
        "rubric_scores": grade["scores"],
        "rubric_score": grade["score"],
        "evaluator_feedback": grade["feedback"],
        "evaluation_method": grade["method"],
        "unmet_requirements": grade["unmet"],
        "final_on_point": grade["on_point"],
        "consensus_agreement": passed,
        "escalated_to_frontier": not passed,
        "models_used": models_used,
        "total_cost": state.get("total_cost", 0.0) + grade["cost"],
        "latency_ms": state.get("latency_ms", 0) + elapsed_ms(start_time)
    })
    return state


def frontier_node(state: AgentState) -> AgentState:
    """Run frontier model when consensus fails. final_check_node decides what is returned."""
    start_time = datetime.now()
    models_used = dict(state.get("models_used") or {})

    final_instruction = (
        "Reply with only the JSON object that is your final answer, matching the required schema."
        if state.get("response_format") else "Provide your final answer:"
    )
    unmet = state.get("unmet_requirements") or []
    frontier_prompt = f"""You are a frontier AI advisor. You have access to:
    1. Original user prompt
    2. Specialist agent output (from cheaper model)
    3. Critic feedback identifying potential issues
    4. Evaluator rubric grade explaining why the answer did not pass
    5. Requirements of the user's request the answer missed

    Your task is to provide a definitive, high-confidence answer that is on point with the user's request:
    it must meet every requirement of that request, including any listed as missed below.
    If the specialist output is sound and criticisms are minor, you may affirm it.
    If criticisms reveal significant issues, you should re-evaluate from scratch or synthesize a better answer.

    User prompt: {state["user_prompt"]}

    Specialist output:
    {state["specialist_output"]}

    Critic feedback:
    {chr(10).join(state["criticisms"])}

    Evaluator grade ({state.get("evaluation_method", "")}): {state.get("rubric_score", 0.0):.2f}/5 (pass >= {PASS_THRESHOLD})
    Scores: {json.dumps(state.get("rubric_scores") or {})}
    Evaluator feedback: {state.get("evaluator_feedback", "")}

    Requirements the answer missed:
    {chr(10).join(f"- {u}" for u in unmet) or "(none listed)"}

    {final_instruction}"""

    # Don't let the model that produced the disputed answer adjudicate it.
    frontier_models = prefer_other_families(
        role_models()["frontier"], [models_used.get("specialist", "")]
    )
    cost = 0.0
    try:
        frontier_output, model, cost = chat(
            frontier_models, frontier_prompt, system=state.get("system"),
            response_format=state.get("response_format"), deadline=state.get("deadline"),
            temperature=0.1,
            **answering_params(state, 4000 if state.get("response_format") else 3000),
        )
        models_used["frontier"] = model
        frontier_error = ""
    except LLMTransportError as e:
        # A disputed answer is still an answer; an error message is not.
        print(f"multi-agent: frontier unavailable, keeping the specialist answer: {e}")
        frontier_output, frontier_error = "", str(e)

    state.update({
        "frontier_output": frontier_output,
        "frontier_error": frontier_error,
        "models_used": models_used,
        "total_cost": state.get("total_cost", 0.0) + cost,
        "latency_ms": state.get("latency_ms", 0) + elapsed_ms(start_time)
    })
    return state


def final_check_node(state: AgentState) -> AgentState:
    """Hold the rewrite to the request too, and return whichever answer is better.

    An answer with no unmet requirement beats one with any; otherwise the
    higher score wins, and a tie goes to the rewrite. Escalation happens once.
    """
    if not state["frontier_output"]:
        state.update({"final_answer": state["specialist_output"], "answer_source": "specialist"})
        return state

    start_time = datetime.now()
    models_used = dict(state.get("models_used") or {})
    grade = grade_answer(
        state, state["frontier_output"],
        [models_used.get("frontier", ""), models_used.get("specialist", "")],
        critic_note=" (about an earlier answer; check whether this one avoids them)",
    )
    if grade["method"] == "rubric":
        models_used["final_evaluator"] = grade["model"]

    use_frontier = True
    if grade["method"] == "rubric" and state["evaluation_method"] == "rubric":
        specialist_rank = (not state["unmet_requirements"], state["rubric_score"])
        frontier_rank = (not grade["unmet"], grade["score"])
        use_frontier = frontier_rank >= specialist_rank

    state.update({
        "frontier_rubric_scores": grade["scores"],
        "frontier_rubric_score": grade["score"],
        "frontier_unmet_requirements": grade["unmet"],
        "frontier_evaluation_method": grade["method"],
        "final_answer": state["frontier_output"] if use_frontier else state["specialist_output"],
        "answer_source": "frontier" if use_frontier else "specialist",
        "final_on_point": grade["on_point"] if use_frontier else state["final_on_point"],
        "models_used": models_used,
        "total_cost": state.get("total_cost", 0.0) + grade["cost"],
        "latency_ms": state.get("latency_ms", 0) + elapsed_ms(start_time)
    })
    return state


def return_specialist(state: AgentState) -> AgentState:
    """Return specialist output when consensus is reached"""
    state["final_answer"] = state["specialist_output"]
    state["answer_source"] = "specialist"
    return state

# ======================
# GRAPH CONSTRUCTION
# ======================

def create_workflow_graph():
    """Create the LangGraph workflow"""
    from langgraph.graph import StateGraph, END

    workflow = StateGraph(AgentState)

    workflow.add_node("route_node", route_node)
    workflow.add_node("specialist_node", specialist_node)
    workflow.add_node("critic_node", critic_node)
    workflow.add_node("evaluate_node", evaluate_node)
    workflow.add_node("frontier_node", frontier_node)
    workflow.add_node("final_check_node", final_check_node)
    workflow.add_node("return_specialist", return_specialist)

    workflow.set_entry_point("route_node")

    workflow.add_edge("route_node", "specialist_node")
    workflow.add_edge("specialist_node", "critic_node")
    workflow.add_edge("critic_node", "evaluate_node")

    workflow.add_conditional_edges(
        "evaluate_node",
        lambda state: state["escalated_to_frontier"],
        {
            True: "frontier_node",
            False: "return_specialist"
        }
    )

    workflow.add_edge("frontier_node", "final_check_node")
    workflow.add_edge("final_check_node", END)
    workflow.add_edge("return_specialist", END)

    return workflow.compile()


def workflow_graph():
    """The compiled graph, built once per process. It holds no per-run state."""
    global _graph
    if _graph is None:
        _graph = create_workflow_graph()
    return _graph

# ======================
# LIBRARY ENTRY POINTS
# ======================

def new_state(prompt: str, system: Optional[str] = None,
              response_format: Optional[dict] = None,
              deadline: Optional[float] = None,
              tier: Optional[Tier] = None,
              reasoning_effort: Optional[str] = None,
              original_prompt: Optional[str] = None) -> AgentState:
    return AgentState(
        user_prompt=prompt,
        original_prompt=original_prompt if original_prompt is not None else prompt,
        system=system,
        response_format=response_format,
        deadline=deadline,
        forced_tier=tier,
        reasoning_effort=reasoning_effort,
        route_decision="cheap",
        route_method="",
        specialist_output="",
        specialist_reasoning="",
        criticisms=[],
        critic_confidence=0.0,
        consensus_agreement=False,
        rubric_scores={},
        rubric_score=0.0,
        evaluator_feedback="",
        evaluation_method="",
        unmet_requirements=[],
        final_answer="",
        answer_source="",
        final_on_point=None,
        escalated_to_frontier=False,
        frontier_output="",
        frontier_error="",
        frontier_rubric_scores={},
        frontier_rubric_score=0.0,
        frontier_unmet_requirements=[],
        frontier_evaluation_method="",
        models_used={},
        total_cost=0.0,
        latency_ms=0,
        timestamp=""
    )


def run(prompt: str, system: Optional[str] = None,
        schema_model: Optional[Type[BaseModel]] = None,
        run_name: Optional[str] = None,
        tier: Optional[str] = None,
        reasoning_effort: Optional[str] = None) -> AgentState:
    """Answer `prompt` through the graph and return the final state.

    With `schema_model`, the answering calls ask for its strict JSON schema;
    final_answer is then JSON text, not yet validated (complete_structured does
    that). `tier` skips the router; `reasoning_effort` goes on the answering
    calls. Raises ValueError for an unknown tier, and LLMTransportError when no
    specialist answers or the MULTI_AGENT_CALL_TIMEOUT budget runs out first.
    """
    if tier is not None and tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; expected one of {', '.join(TIERS)}")
    original_prompt = prompt
    if schema_model:
        # Not every proxied model enforces response_format, so say it in words too.
        schema = json.dumps(schema_model.model_json_schema())
        prompt = (f"{prompt}\n\nReply with only a JSON object that matches this "
                  f"JSON schema, with every property present:\n{schema}")
    state = new_state(
        prompt, system=system,
        response_format=json_schema_format(schema_model) if schema_model else None,
        deadline=time.monotonic() + run_timeout(),
        tier=tier, reasoning_effort=reasoning_effort, original_prompt=original_prompt,
    )
    final_state = workflow_graph().invoke(state)
    print(describe_run(final_state, run_name or "multi_agent"))
    if _mlflow_enabled():
        log_to_mlflow(final_state, run_name or "multi_agent")
    return final_state


def describe_run(state: AgentState, run_name: str) -> str:
    """One log line saying who answered and how the answer was judged."""
    grade = (f"{state['rubric_score']:.2f}" if state["evaluation_method"] == "rubric"
             else state["evaluation_method"])
    models = " ".join(f"{role}={model}" for role, model in state["models_used"].items())
    line = (f"multi-agent {run_name}: tier={state['route_decision']} ({state['route_method']}) "
            f"effort={state.get('reasoning_effort') or 'default'} grade={grade} "
            f"escalated={state['escalated_to_frontier']}")
    if state["escalated_to_frontier"] and state.get("frontier_evaluation_method"):
        frontier_grade = (f"{state['frontier_rubric_score']:.2f}"
                          if state["frontier_evaluation_method"] == "rubric"
                          else state["frontier_evaluation_method"])
        line += f" frontier_grade={frontier_grade}"
    line += f" chosen={state['answer_source']} on_point={state['final_on_point']}"
    unmet = (state["frontier_unmet_requirements"] if state["answer_source"] == "frontier"
             and state["escalated_to_frontier"] else state["unmet_requirements"])
    if unmet:
        line += f" unmet={'; '.join(unmet)[:300]}"
    line += f" {models}"
    if state.get("frontier_error"):
        line += f" frontier_error={state['frontier_error'][:200]}"
    return line


def complete_structured(prompt: str, schema_model: Type[BaseModel],
                        system: Optional[str] = None, max_attempts: int = 2,
                        tier: Optional[str] = None,
                        reasoning_effort: Optional[str] = None) -> Any:
    """flow.llm.complete_structured's contract, answered by the graph.

    The graph runs once. An answer that fails validation gets up to
    max_attempts - 1 repair calls carrying the errors -- re-running the whole
    graph per attempt would cost five or more calls each time.
    """
    state = run(prompt, system=system, schema_model=schema_model,
                run_name=schema_model.__name__, tier=tier,
                reasoning_effort=reasoning_effort)
    content = state["final_answer"]
    tier_models = role_models()[state["route_decision"]]
    answered_by = state["models_used"].get(state["answer_source"] or "specialist")
    repair_models = [answered_by, *[m for m in tier_models if m != answered_by]] if answered_by else tier_models
    # The graph's prompt, which carries the schema in words; the bare argument doesn't.
    prior: List[dict] = [{"role": "user", "content": state["user_prompt"]}]

    last_error: Exception = ValueError("no attempt made")
    for attempt in range(1, max_attempts + 1):
        try:
            return schema_model.model_validate_json(content)
        except (ValidationError, json.JSONDecodeError) as exc:
            last_error = exc
            print(f"multi-agent answer failed validation (attempt {attempt}): {exc}")
        if attempt == max_attempts:
            break
        prior.append({"role": "assistant", "content": content})
        repair_prompt = ("That did not validate against the schema. Errors:\n"
                         f"{last_error}\nReturn only JSON matching the schema.")
        content, repaired_by, _ = chat(
            repair_models, repair_prompt,
            system=system, response_format=json_schema_format(schema_model),
            deadline=state.get("deadline"), prior=prior, temperature=0,
            **answering_params(state, 4000),
        )
        print(f"multi-agent {schema_model.__name__}: repair attempt {attempt} by {repaired_by}")
        prior.append({"role": "user", "content": repair_prompt})

    raise LLMSchemaError(
        f"{schema_model.__name__} not produced by the multi-agent graph in "
        f"{max_attempts} attempts: {last_error}"
    )

# ======================
# MLFLOW
# ======================

def _mlflow_enabled() -> bool:
    return os.environ.get("MULTI_AGENT_MLFLOW", "").strip().lower() in ("1", "true", "yes", "on")


def log_to_mlflow(final_state: AgentState, run_name: str) -> None:
    """Record one run. Nested, so it can sit inside a caller's active run."""
    # Local file store unless the caller configured a tracking server.
    os.environ.setdefault("MLFLOW_TRACKING_URI", "file:///tmp/mlflow")
    os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    import mlflow

    try:
        with mlflow.start_run(run_name=run_name, nested=True):
            mlflow.log_param("prompt", final_state["user_prompt"][:500])
            mlflow.log_param("route_decision", final_state["route_decision"])
            mlflow.log_param("route_method", final_state["route_method"])
            mlflow.log_param("escalated_to_frontier", final_state["escalated_to_frontier"])
            mlflow.log_param("consensus_agreement", final_state["consensus_agreement"])
            mlflow.log_param("answer_source", final_state["answer_source"])
            mlflow.log_param("final_on_point", final_state["final_on_point"])
            mlflow.log_metric("unmet_requirements", len(final_state["unmet_requirements"]))
            if final_state["frontier_evaluation_method"] == "rubric":
                mlflow.log_metric("frontier_rubric_score", final_state["frontier_rubric_score"])
            for role, model in final_state["models_used"].items():
                mlflow.log_param(f"model_{role}", model)
            mlflow.log_metric("total_cost", final_state["total_cost"])
            mlflow.log_metric("latency_ms", final_state["latency_ms"])
            mlflow.log_metric("critic_confidence", final_state["critic_confidence"])
            mlflow.log_param("evaluation_method", final_state["evaluation_method"])
            mlflow.log_metric("rubric_score", final_state["rubric_score"])
            for criterion, score in final_state["rubric_scores"].items():
                mlflow.log_metric(f"rubric_{criterion}", score)
    except Exception as e:
        # Tracking is observability; it must not fail the answer it describes.
        print(f"mlflow logging failed: {type(e).__name__}: {e}", file=sys.stderr)
