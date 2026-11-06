"""Workflow conftest: auto-clean tables before every test in this folder."""
import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def _clean_for_workflow(clean_tables):
    yield
