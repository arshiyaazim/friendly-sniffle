"""Integration-layer conftest: auto-clean tables before every test in this folder."""
import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def _clean_for_integration(clean_tables):
    """Delegate to the root clean_tables fixture (autouse for integration tests)."""
    yield
