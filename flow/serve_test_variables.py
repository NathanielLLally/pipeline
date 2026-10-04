"""
Serve the test_prefect_variables flow as a deployment.

    python flow/serve_test_variables.py

Then trigger:
    prefect deployment run test-prefect-variables/test-prefect-variables
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prefect import serve

from flow.test_prefect_variables import test_prefect_variables

if __name__ == "__main__":
    deployment = test_prefect_variables.to_deployment(
        name="test-prefect-variables",
        description="Test Prefect variables: set/get, storage, overwrite, pretty-print",
    )
    serve(deployment)
