"""
Guards on the @flow signatures themselves.

Prefect validates a flow run's parameters against the function's annotations,
so `offer: str = None` is accepted by Python, passes every test that calls
`.fn`, and then fails at run time with
"ParameterTypeError: offer: Input should be a valid string".

Unit tests cannot catch this because they deliberately bypass Prefect. These
tests inspect the annotations directly instead.
"""

import typing

import pytest

from flow.agents.drafting import drafting_agent
from flow.agents.research import research_agent
from flow.agents.selector import candidate_selector
from flow.run_agents import run_agents

FLOWS = [run_agents, research_agent, drafting_agent, candidate_selector]


def _hints(prefect_flow):
    return typing.get_type_hints(prefect_flow.fn)


def _accepts_none(annotation) -> bool:
    return type(None) in typing.get_args(annotation)


@pytest.mark.parametrize("prefect_flow", FLOWS, ids=lambda f: f.name)
def test_none_defaulted_parameters_are_optional(prefect_flow):
    """A parameter defaulting to None must be annotated to accept None."""
    import inspect

    signature = inspect.signature(prefect_flow.fn)
    hints = _hints(prefect_flow)

    offenders = [
        name for name, param in signature.parameters.items()
        if param.default is None
        and name in hints
        and not _accepts_none(hints[name])
    ]

    assert offenders == [], (
        f"{prefect_flow.name}: {offenders} default to None but are not "
        "Optional; Prefect will reject the run with ParameterTypeError"
    )


@pytest.mark.parametrize("prefect_flow", FLOWS, ids=lambda f: f.name)
def test_every_parameter_is_annotated(prefect_flow):
    """Prefect builds the UI parameter form from these annotations."""
    import inspect

    signature = inspect.signature(prefect_flow.fn)
    unannotated = [n for n, p in signature.parameters.items()
                   if p.annotation is inspect.Parameter.empty]

    assert unannotated == []
