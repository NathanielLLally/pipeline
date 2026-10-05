"""
Artifact I/O: persist flow inputs/outputs as JSON files tagged by flow run.

Files are named by sorting `prefect.runtime.flow_run.tags` and joining with '-':
  input:  {tags_joined}-input.json
  output: {tags_joined}-output.json

Tags are set at the *call site*, not inside the agent -- see the
`with tags(business_id, stage)` wrappers in flow/run_agents.py. An agent
invoked without them writes to 'unknown-input.json'/'unknown-output.json'.

This is useful for:
- Debugging: inspect what was fed to each agent
- Replay: load a previous run's input JSON to re-run with identical params
- Audit: link each output back to its exact input via tags
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect.runtime import flow_run
from slugify import slugify


def variable_safe_name(raw: str) -> str:
    """
    Transliterate `raw` into a name Prefect's VariableCreate will accept.

    A Prefect Variable name must be lowercase letters, numbers and
    underscores only -- no dashes, despite what the Prefect docs say
    (https://docs.prefect.io/v3/concepts/variables lists dashes as allowed;
    the actual VariableCreate validator rejects them). A raw UUID
    business_id ('94cc25a6-c778-...') or an unsanitized business_name both
    fail it, and the call site's own try/except then swallows the error --
    see 'research_agent_output_{business_id}' in flow/agents/research.py.

    slugify handles this in one pass: it lowercases, transliterates
    non-ASCII (so a Café/Münich-style business name degrades to ASCII
    instead of failing or getting silently dropped), and replaces every
    separator -- dashes, spaces, punctuation -- with '_'.
    """
    return slugify(raw, separator='_', lowercase=True)


def artifact_filename(suffix: str = 'output') -> str:
    """
    Build a filename from flow run tags.

    Args:
        suffix: 'input' or 'output' (or any other suffix)

    Returns:
        '{tag1}-{tag2}-{suffix}.json', tags sorted.
        Falls back to 'unknown-{suffix}.json' if no tags.

    Example:
        With tags=['biz-1', 'research']:
            artifact_filename('input')  -> 'biz-1-research-input.json'
            artifact_filename('output') -> 'biz-1-research-output.json'

    Tags are sorted because Prefect holds run tags in a *set*
    (prefect.context.tags unions into TagsContext.current_tags, which
    FlowRunContext carries through to flow_run.tags), so they arrive in
    arbitrary order. Without sorting, one run writes
    'biz-1-research-output.json' and the next writes
    'research-biz-1-output.json' for the same business and stage, which
    defeats the replay and audit uses described at the top of this module.
    """
    tags = flow_run.tags or []
    if tags:
        tag_str = '-'.join(sorted(str(t) for t in tags))
        return f'{tag_str}-{suffix}.json'
    return f'unknown-{suffix}.json'


def write_artifact(data: Dict[str, Any], suffix: str = 'output',
                   directory: Optional[Path] = None) -> Path:
    """
    Write a JSON artifact tagged by flow run.

    Args:
        data: dict to serialize
        suffix: 'input' or 'output'
        directory: where to write (default: current directory)

    Returns:
        Path to the written file

    Raises:
        ValueError: if data cannot be JSON-serialized
    """
    if directory is None:
        directory = Path('.')
    else:
        directory = Path(directory)

    filename = artifact_filename(suffix)
    filepath = directory / filename

    try:
        json_str = json.dumps(data, indent=2, default=str)
    except (TypeError, ValueError) as e:
        raise ValueError(f'data is not JSON-serializable: {e}') from e

    filepath.write_text(json_str)
    return filepath


def read_artifact(filepath: str, suffix: Optional[str] = None) -> Dict[str, Any]:
    """
    Read a JSON artifact.

    Args:
        filepath: path to the JSON file. Can be absolute or relative.
                 If suffix is provided and filepath has no extension,
                 append '-{suffix}.json'.
        suffix: optional suffix to append if filepath has no extension
                (e.g. 'input', 'output')

    Returns:
        Parsed JSON dict

    Raises:
        FileNotFoundError: if file does not exist
        json.JSONDecodeError: if file is not valid JSON
    """
    path = Path(filepath)

    # If no extension, try appending suffix
    if path.suffix == '' and suffix:
        path = path.with_name(f'{path.name}-{suffix}.json')

    if not path.exists():
        raise FileNotFoundError(f'Artifact not found: {path}')

    content = path.read_text()
    return json.loads(content)
