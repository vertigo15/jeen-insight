"""Unit tests for the per-user concurrency governor."""

from __future__ import annotations

import asyncio

import pytest

from src.api.concurrency import ConcurrencyLimitExceeded, UserConcurrencyLimiter


class TestDisabled:
    @pytest.mark.asyncio
    async def test_zero_disables(self):
        lim = UserConcurrencyLimiter(max_per_user=0)
        assert lim.enabled is False
        # Never raises, never tracks.
        for _ in range(10):
            await lim.acquire("u")
        assert lim.active("u") == 0
        await lim.release("u")


class TestRejectImmediately:
    @pytest.mark.asyncio
    async def test_allows_up_to_limit(self):
        lim = UserConcurrencyLimiter(max_per_user=2)
        await lim.acquire("u")
        await lim.acquire("u")
        assert lim.active("u") == 2

    @pytest.mark.asyncio
    async def test_rejects_over_limit(self):
        lim = UserConcurrencyLimiter(max_per_user=1)
        await lim.acquire("u")
        with pytest.raises(ConcurrencyLimitExceeded):
            await lim.acquire("u")

    @pytest.mark.asyncio
    async def test_release_frees_slot(self):
        lim = UserConcurrencyLimiter(max_per_user=1)
        await lim.acquire("u")
        await lim.release("u")
        assert lim.active("u") == 0
        await lim.acquire("u")  # slot free again → no raise

    @pytest.mark.asyncio
    async def test_users_are_independent(self):
        lim = UserConcurrencyLimiter(max_per_user=1)
        await lim.acquire("a")
        await lim.acquire("b")  # different user → own budget
        assert lim.active("a") == 1
        assert lim.active("b") == 1


class TestSlotContextManager:
    @pytest.mark.asyncio
    async def test_slot_acquires_and_releases(self):
        lim = UserConcurrencyLimiter(max_per_user=1)
        async with lim.slot("u"):
            assert lim.active("u") == 1
        assert lim.active("u") == 0

    @pytest.mark.asyncio
    async def test_slot_releases_on_exception(self):
        lim = UserConcurrencyLimiter(max_per_user=1)
        with pytest.raises(ValueError):
            async with lim.slot("u"):
                raise ValueError("boom")
        assert lim.active("u") == 0


class TestWaitTimeout:
    @pytest.mark.asyncio
    async def test_waits_then_acquires_when_freed(self):
        lim = UserConcurrencyLimiter(max_per_user=1, wait_timeout=1.0)
        await lim.acquire("u")

        async def _release_soon():
            await asyncio.sleep(0.05)
            await lim.release("u")

        asyncio.create_task(_release_soon())
        # Should block briefly, then succeed once the slot frees.
        await lim.acquire("u")
        assert lim.active("u") == 1

    @pytest.mark.asyncio
    async def test_times_out_when_never_freed(self):
        lim = UserConcurrencyLimiter(max_per_user=1, wait_timeout=0.05)
        await lim.acquire("u")
        with pytest.raises(ConcurrencyLimitExceeded):
            await lim.acquire("u")
