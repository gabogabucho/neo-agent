"""Pytest configuration."""

import pytest

# Register asyncio marker for compatibility
def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: mark test as async")
