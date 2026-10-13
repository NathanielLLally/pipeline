#!/usr/bin/env python3
"""
Interactive / one-shot CLI for the multi-agent system in flow/multi_agent.py.

    $ python multi_agent_system.py "explain board-and-train pricing"
    $ python multi_agent_system.py            # prompt loop

Every run is logged to MLflow (file:///tmp/mlflow unless MLFLOW_TRACKING_URI is set).
To use the same graph from the pipeline agents, set LLM_MODEL or RESEARCH_MODEL /
DRAFTING_MODEL / ANALYSIS_MODEL to 'multi-agent'.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("MULTI_AGENT_MLFLOW", "1")

from flow.llm import LLMTransportError
from flow.multi_agent import run


def run_prompt(prompt: str, idx: int) -> None:
    try:
        final_state = run(prompt, run_name=f"multi_agent_system_prompt_{idx}")
    except LLMTransportError as e:
        print(f"\nfailed: {e}\n", file=sys.stderr)
        return

    print(f"\nRoute: {final_state['route_decision']} ({final_state['route_method']})")
    for role, model in final_state["models_used"].items():
        print(f"  {role:<10} {model}")
    print(f"Criticisms: {len(final_state['criticisms'])}")
    for crit in final_state['criticisms'][:3]:
        print(f"  - {crit[:200]}")
    if final_state["rubric_scores"]:
        scores = " ".join(f"{c}={s}" for c, s in final_state["rubric_scores"].items())
        print(f"Rubric: {final_state['rubric_score']:.2f}/5 ({scores})")
    else:
        print(f"Rubric: n/a ({final_state['evaluation_method']})")
    if final_state["evaluator_feedback"]:
        print(f"  {final_state['evaluator_feedback'][:300]}")
    if final_state["escalated_to_frontier"] and final_state["frontier_evaluation_method"] == "rubric":
        print(f"Frontier rubric: {final_state['frontier_rubric_score']:.2f}/5")
    unmet = (final_state["frontier_unmet_requirements"] if final_state["answer_source"] == "frontier"
             and final_state["escalated_to_frontier"] else final_state["unmet_requirements"])
    print(f"Answer: {final_state['answer_source']} | on point: {final_state['final_on_point']}")
    for requirement in unmet:
        print(f"  unmet: {requirement[:200]}")
    if final_state["frontier_error"]:
        print(f"Frontier failed, kept specialist answer: {final_state['frontier_error'][:300]}")
    print(f"Passed: {final_state['consensus_agreement']} | escalated: {final_state['escalated_to_frontier']}")
    print(f"Cost: ${final_state['total_cost']:.4f} | latency: {final_state['latency_ms']}ms")
    print(f"\n{final_state['final_answer']}\n")


def main():
    """Interactive prompt loop. Prompts given as arguments are run once, non-interactively."""
    if len(sys.argv) > 1:
        for idx, prompt in enumerate(sys.argv[1:]):
            run_prompt(prompt, idx)
        return

    import readline  # enables line editing and history for input()

    history_file = os.path.expanduser("~/.multi_agent_history")
    try:
        readline.read_history_file(history_file)
    except (FileNotFoundError, OSError):
        pass
    readline.set_history_length(1000)

    print("Multi-agent prompt. 'exit', 'quit' or Ctrl-D to leave; Ctrl-C cancels.")
    idx = 0
    try:
        while True:
            try:
                prompt = input("> ").strip()
            except KeyboardInterrupt:
                print()
                continue
            except EOFError:
                print()
                break
            if not prompt:
                continue
            if prompt.lower() in ("exit", "quit"):
                break
            try:
                run_prompt(prompt, idx)
            except KeyboardInterrupt:
                print("\n(cancelled)")
            idx += 1
    finally:
        try:
            readline.write_history_file(history_file)
        except OSError:
            pass


if __name__ == "__main__":
    main()
