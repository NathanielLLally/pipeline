"""
Artifact I/O: persist flow inputs/outputs as JSON files tagged by flow run.

Files are named by joining `prefect.runtime.flow_run.tags` with '-':
  input:  {tags_joined}-input.json
  output: {tags_joined}-output.json

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


def artifact_filename(suffix: str = 'output') -> str:
    """
    Build a filename from flow run tags.

    Args:
        suffix: 'input' or 'output' (or any other suffix)

    Returns:
        '{tag1}-{tag2}-{suffix}.json'
        Falls back to 'unknown-{suffix}.json' if no tags.

    Example:
        With tags=['biz-1', 'research']:
            artifact_filename('input')  -> 'biz-1-research-input.json'
            artifact_filename('output') -> 'biz-1-research-output.json'
    """
    tags = flow_run.tags or []
    if tags:
        tag_str = '-'.join(str(t) for t in tags)
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
