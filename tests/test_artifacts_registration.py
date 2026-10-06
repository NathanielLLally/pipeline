"""
Guards on artifact registration.

A real run logged, twice:

    Note: could not register artifact input: cannot import name
    'create_artifact' from 'prefect.artifacts'

`create_artifact` does not exist in Prefect 3.2.15 -- the module exports
create_markdown_artifact, create_table_artifact, create_link_artifact,
create_image_artifact, create_progress_artifact and the Artifact class. The
wrong name was imported INSIDE the try/except, so a coding error became a
silent no-op that read exactly like "no Prefect API available", and the
structured JSON was never recorded.

These tests pin the distinction: wrong code must fail loudly at import,
while a genuinely absent API stays tolerated at runtime.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestTheSymbolExists:
    """The specific regression: importing a name Prefect does not export."""

    def test_prefect_does_not_export_create_artifact(self):
        """If a future Prefect adds it, this test should be revisited."""
        import prefect.artifacts

        assert not hasattr(prefect.artifacts, 'create_artifact')

    def test_artifacts_module_neither_imports_nor_calls_it(self):
        """Comments may name it; code must not import or call it."""
        import pathlib

        source = pathlib.Path('flow/artifacts.py').read_text()

        assert 'import create_artifact' not in source
        assert 'create_artifact(' not in source

    def test_the_symbol_we_use_is_imported_at_module_scope(self):
        """Module-scope import means a wrong name is a loud ImportError.

        Inside a try/except it is indistinguishable from a missing API, which
        is exactly how this went unnoticed through a full pipeline run.
        """
        import flow.artifacts as mod

        assert hasattr(mod, 'Artifact'), (
            'Artifact should be imported at module scope so a wrong symbol '
            'fails at import rather than being swallowed at call time'
        )


class TestRegistrationBehaviour:
    def test_registers_with_the_artifact_class(self):
        import flow.artifacts as mod

        with patch.object(mod, 'Artifact') as artifact:
            mod.register_artifact({'a': 1}, suffix='output')

        artifact.assert_called_once()
        kwargs = artifact.call_args.kwargs
        assert kwargs['data'] == {'a': 1}
        assert kwargs['type'] == 'result'
        artifact.return_value.create.assert_called_once()

    def test_a_missing_api_is_still_tolerated(self):
        """No API is an environment fact, not a bug: keep swallowing it."""
        import flow.artifacts as mod

        with patch.object(mod, 'Artifact') as artifact:
            artifact.return_value.create.side_effect = RuntimeError(
                'no Prefect API')
            mod.register_artifact({'a': 1}, suffix='output')  # must not raise

    def test_key_is_lowercase_and_dash_separated(self):
        """Prefect rejects keys outside [a-z0-9-]; tags are free-form."""
        import flow.artifacts as mod

        with patch.object(mod, 'Artifact') as artifact, \
             patch.object(mod.flow_run, 'tags', ['Biz_ID 42', 'Tier 1']):
            mod.register_artifact({'a': 1}, suffix='output')

        key = artifact.call_args.kwargs['key']
        assert key == key.lower()
        assert all(c.isalnum() or c == '-' for c in key), key
