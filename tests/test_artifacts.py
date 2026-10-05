"""Unit tests for artifact I/O utilities."""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flow.artifacts import (
    artifact_filename,
    read_artifact,
    variable_safe_name,
    write_artifact,
)


class TestVariableSafeName:
    """Prefect Variable names allow only lowercase letters, numbers and
    underscores -- a raw UUID business_id ('94cc25a6-c778-...') fails
    VariableCreate's validator because of its dashes. slugify transliterates
    non-ASCII too, not just dashes, so names stay valid regardless of what a
    business_id or business_name happens to contain.
    """

    def test_dashes_become_underscores(self):
        name = variable_safe_name('research_agent_output_94cc25a6-c778-4156')
        assert name == 'research_agent_output_94cc25a6_c778_4156'

    def test_result_contains_only_lowercase_alnum_and_underscore(self):
        import re

        name = variable_safe_name('Research-Agent_Output-Biz#1')
        assert re.fullmatch(r'[a-z0-9_]+', name)

    def test_uppercase_is_lowercased(self):
        assert variable_safe_name('ACME-Corp') == 'acme_corp'

    def test_unicode_is_transliterated_not_dropped(self):
        name = variable_safe_name('research_output_Café_Münich')
        assert name == 'research_output_cafe_munich'

    def test_plain_uuid_round_trips_safely(self):
        uuid = '94cc25a6-c778-4156-a21c-7a4f0620503f'
        name = variable_safe_name(f'research_agent_output_{uuid}')
        assert '-' not in name
        assert name == 'research_agent_output_94cc25a6_c778_4156_a21c_7a4f0620503f'


class TestArtifactFilename:
    """Test artifact filename generation from flow run tags."""

    def test_single_tag_input(self):
        """Single tag + suffix -> {tag}-{suffix}.json"""
        with patch('flow.artifacts.flow_run.tags', ['biz-1']):
            assert artifact_filename('input') == 'biz-1-input.json'

    def test_multiple_tags_output(self):
        """Multiple tags joined with '-' + suffix"""
        with patch('flow.artifacts.flow_run.tags', ['biz-1', 'research']):
            assert artifact_filename('output') == 'biz-1-research-output.json'

    def test_three_tags(self):
        """Three tags all joined"""
        with patch('flow.artifacts.flow_run.tags', ['biz-42', 'research', 'v2']):
            assert artifact_filename('output') == 'biz-42-research-v2-output.json'

    def test_no_tags_falls_back(self):
        """No tags -> 'unknown-{suffix}.json'"""
        with patch('flow.artifacts.flow_run.tags', None):
            assert artifact_filename('output') == 'unknown-output.json'

    def test_empty_tags_falls_back(self):
        """Empty tag list -> 'unknown-{suffix}.json'"""
        with patch('flow.artifacts.flow_run.tags', []):
            assert artifact_filename('output') == 'unknown-output.json'

    def test_custom_suffix(self):
        """Custom suffix instead of input/output"""
        with patch('flow.artifacts.flow_run.tags', ['biz-1']):
            assert artifact_filename('metadata') == 'biz-1-metadata.json'

    def test_name_is_stable_regardless_of_tag_order(self):
        """
        Prefect stores run tags in a set, so flow_run.tags arrives in
        arbitrary order (prefect.context.tags does current_tags.union(...),
        and FlowRunContext carries that set through to flow_run.tags). If the
        filename followed that order, the same business+stage would land in
        'biz-1-research-output.json' on one run and
        'research-biz-1-output.json' on the next, which defeats the replay
        and audit uses this module exists for.
        """
        with patch('flow.artifacts.flow_run.tags', ['research', 'biz-1']):
            one = artifact_filename('output')
        with patch('flow.artifacts.flow_run.tags', ['biz-1', 'research']):
            two = artifact_filename('output')

        assert one == two

    def test_name_is_stable_when_tags_arrive_as_a_set(self):
        """A set is what Prefect actually hands over at runtime."""
        names = set()
        for _ in range(10):
            with patch('flow.artifacts.flow_run.tags', {'biz-1', 'research'}):
                names.add(artifact_filename('output'))

        assert names == {'biz-1-research-output.json'}


class TestWriteArtifact:
    """Test writing JSON artifacts."""

    def test_writes_valid_json(self):
        """Writes a dict as formatted JSON"""
        with TemporaryDirectory() as tmpdir:
            data = {'name': 'test', 'value': 42}
            with patch('flow.artifacts.flow_run.tags', ['test']):
                filepath = write_artifact(data, suffix='output', directory=tmpdir)

            assert filepath.exists()
            assert json.loads(filepath.read_text()) == data

    def test_returns_path(self):
        """Returns the path to the written file"""
        with TemporaryDirectory() as tmpdir:
            data = {'x': 1}
            with patch('flow.artifacts.flow_run.tags', ['biz']):
                result = write_artifact(data, directory=tmpdir)

            assert isinstance(result, Path)
            assert result.name == 'biz-output.json'

    def test_indented_json(self):
        """JSON is pretty-printed with 2-space indent"""
        with TemporaryDirectory() as tmpdir:
            data = {'a': {'b': 'c'}}
            with patch('flow.artifacts.flow_run.tags', ['test']):
                filepath = write_artifact(data, directory=tmpdir)

            content = filepath.read_text()
            assert '  ' in content  # indented
            assert '{\n' in content  # formatted

    def test_stringifies_non_serializable_types(self):
        """Non-JSON types are stringified via default=str"""
        from datetime import datetime

        with TemporaryDirectory() as tmpdir:
            data = {'timestamp': datetime(2026, 10, 4, 12, 0, 0)}
            with patch('flow.artifacts.flow_run.tags', ['test']):
                filepath = write_artifact(data, directory=tmpdir)

            loaded = json.loads(filepath.read_text())
            assert isinstance(loaded['timestamp'], str)
            assert '2026-10-04' in loaded['timestamp']

    def test_raises_on_circular_reference(self):
        """Raises ValueError for non-serializable data"""
        with TemporaryDirectory() as tmpdir:
            circular = {'x': 1}
            circular['self'] = circular  # circular reference
            with patch('flow.artifacts.flow_run.tags', ['test']):
                with pytest.raises(ValueError, match='not JSON-serializable'):
                    write_artifact(circular, directory=tmpdir)

    def test_uses_current_directory_by_default(self, tmp_path):
        """Writes to current directory if directory=None"""
        import os
        cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            data = {'test': 'data'}
            with patch('flow.artifacts.flow_run.tags', ['x']):
                result = write_artifact(data, suffix='out')

            assert result.resolve().parent == tmp_path.resolve()
            assert (tmp_path / 'x-out.json').exists()
        finally:
            os.chdir(cwd)


class TestReadArtifact:
    """Test reading JSON artifacts."""

    def test_reads_existing_file(self):
        """Reads and parses JSON file"""
        with TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / 'test-input.json'
            data = {'name': 'test', 'value': 42}
            filepath.write_text(json.dumps(data))

            result = read_artifact(str(filepath))

            assert result == data

    def test_appends_suffix_to_extensionless_path(self):
        """If filepath has no extension, append '-{suffix}.json'"""
        with TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / 'biz-1-research-input.json'
            data = {'x': 1}
            filepath.write_text(json.dumps(data))

            # Read by path without extension; it will append '-input.json'
            result = read_artifact(str(Path(tmpdir) / 'biz-1-research'), suffix='input')

            assert result == data

    def test_raises_on_missing_file(self):
        """Raises FileNotFoundError for nonexistent file"""
        with pytest.raises(FileNotFoundError, match='Artifact not found'):
            read_artifact('/nonexistent/path.json')

    def test_raises_on_invalid_json(self):
        """Raises json.JSONDecodeError for malformed JSON"""
        with TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / 'bad.json'
            filepath.write_text('not valid json {')

            with pytest.raises(json.JSONDecodeError):
                read_artifact(str(filepath))

    def test_accepts_absolute_path(self):
        """Works with absolute paths"""
        with TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir).resolve() / 'test.json'
            data = {'test': 'data'}
            filepath.write_text(json.dumps(data))

            result = read_artifact(str(filepath))

            assert result == data

    def test_accepts_relative_path(self):
        """Works with relative paths"""
        import os
        cwd = os.getcwd()
        try:
            with TemporaryDirectory() as tmpdir:
                os.chdir(tmpdir)
                filepath = Path('test.json')
                data = {'test': 'data'}
                filepath.write_text(json.dumps(data))

                result = read_artifact('test.json')

                assert result == data
        finally:
            os.chdir(cwd)


class TestRoundTrip:
    """Test write then read."""

    def test_write_then_read_preserves_data(self):
        """Data survives write -> read cycle"""
        with TemporaryDirectory() as tmpdir:
            original = {
                'business_id': 'biz-123',
                'research': {
                    'confidence': 0.92,
                    'pain_signals': ['no booking', 'no form'],
                },
                'draft': {
                    'selected_emails': ['owner@example.com'],
                    'subject': 'test',
                },
            }
            with patch('flow.artifacts.flow_run.tags', ['biz-123', 'full']):
                filepath = write_artifact(original, suffix='output', directory=tmpdir)
                loaded = read_artifact(str(filepath))

            assert loaded == original

    def test_complex_nested_structure(self):
        """Works with deeply nested structures"""
        with TemporaryDirectory() as tmpdir:
            data = {
                'level1': {
                    'level2': {
                        'level3': [
                            {'a': 1, 'b': [1, 2, 3]},
                            {'c': 'string'},
                        ]
                    }
                }
            }
            with patch('flow.artifacts.flow_run.tags', ['complex']):
                filepath = write_artifact(data, directory=tmpdir)
                loaded = read_artifact(str(filepath))

            assert loaded == data
