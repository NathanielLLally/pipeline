"""
Both webhook modules are started by hand as scripts on the prod host:

    $ python flow/warmbly_http_endpoint.py

That invocation puts `flow/` on sys.path rather than the repo root, so an
absolute `from flow.x import ...` raises ModuleNotFoundError even though the
test suite imports it fine (pytest adds the rootdir). These tests pin the
script invocation, because the failure only appears outside pytest.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("script", [
    "flow/warmbly_http_endpoint.py",
    "flow/warmbly_webhook_receiver.py",
])
def test_script_imports_without_module_error(script):
    """Starting the file directly must not fail on imports.

    Both scripts then block (uvicorn / serve), so a short timeout is the success
    signal: we only care that the import phase completed.
    """
    proc = subprocess.Popen(
        [sys.executable, script],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = proc.communicate(timeout=12)
        # Exited rather than blocking. Only import-phase failures are this
        # test's business: a blanket 'no Traceback' assertion also catches
        # runtime problems that have nothing to do with sys.path -- notably
        # Prefect spinning up an ephemeral API server (no PREFECT_API_URL in
        # the subprocess env) which returns 500 under this Python.
        for import_failure in (
            'ModuleNotFoundError',
            'ImportError',
            'IndentationError',
            'SyntaxError',
        ):
            assert import_failure not in output, (
                f'{script} failed to import: {import_failure}\n'
                f'{output[-800:]}'
            )
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        # Still running after the import phase: success.
