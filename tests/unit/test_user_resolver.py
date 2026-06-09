"""Tests for SimpleUserResolver."""

import pytest

from src.agent.user_resolver import SimpleUserResolver


@pytest.mark.asyncio
async def test_resolve_user_from_context():
    resolver = SimpleUserResolver()
    user = await resolver.resolve_user(
        {"user_id": "42", "user_name": "Ada", "user_email": "ada@example.com"}
    )
    assert user.id == "42"
    assert user.name == "Ada"
    assert user.email == "ada@example.com"


@pytest.mark.asyncio
async def test_resolve_user_defaults_without_context():
    resolver = SimpleUserResolver(default_user_id="default")
    user = await resolver.resolve_user({})
    assert user.id == "default"
