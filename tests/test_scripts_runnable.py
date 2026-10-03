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
        # Exited rather than blocking -- that means it failed.
        assert 'ModuleNotFoundError' not in output, output[-800:]
        assert 'Traceback' not in output, output[-800:]
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        # Still running after the import phase: success.
