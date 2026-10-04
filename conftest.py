"""
Make the repo root importable so `from flow... import ...` works under a bare
`pytest` invocation.

`python -m pytest` happens to work without this because `python -m` prepends
the CWD to sys.path; the `pytest` console script does not. Every test module
here imports `flow.*`, so without this file the suite only runs one of the two
ways people actually type it.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def pytest_report_header(config):
    """
    Say which Prefect the suite is talking to.

    With PREFECT_API_URL unset, the client spins up a throwaway in-process
    server per test instead of using the one on :4200. That fallback is silent
    and, on the installed fastapi/prefect pair, fails every variable call with
    an HTTP 500 -- which reads as 22 broken tests rather than a missing env
    var. Printing the target makes the difference obvious in the first line of
    output. The address stays in the environment; nothing is hardcoded here.
    """
    url = os.getenv('PREFECT_API_URL')
    if url:
        return f'prefect api: {url}'
    return ('prefect api: UNSET -- using a throwaway in-process server; '
            'variable-backed tests will fail. Set PREFECT_API_URL.')
