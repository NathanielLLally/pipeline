"""Pytest configuration and custom options."""

import pytest


def pytest_addoption(parser):
    """Add custom command-line option to include integration tests."""
    parser.addoption(
        "--all",
        action="store_true",
        default=False,
        help="run all tests including integration tests (default: unit tests only)",
    )


def pytest_configure(config):
    """Configure pytest based on --all flag."""
    if not config.getoption("--all"):
        # By default, ignore integration tests
        if config.option.ignore is None:
            config.option.ignore = []
        config.option.ignore.append("integration")


def pytest_collection_modifyitems(config, items):
    """Allow --all flag to override default behavior."""
    if config.getoption("--all"):
        # When --all is specified, allow integration tests
        pass
