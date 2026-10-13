"""Tests for the QR login flow."""
import pytest
from unittest.mock import AsyncMock, patch

from max_bridge.max_client import OpCode
from max_bridge.qr_login import QrExpiredError, QrLoginError, run_qr_flow


def qr(n: int, ttl: int = 120_000) -> dict:
    return {
        "qrLink": f"https://max.ru/:auth/{n}", "trackId": f"track{n}",
        "ttl": ttl, "pollingInterval": 5000, "expiresAt": 0,
    }


def scripted(*responses):
    """A fake MAX connection answering calls in order; records what was asked."""
    calls = []
    queue = list(responses)

    async def call(opcode, payload):
        calls.append((opcode, payload))
        return queue.pop(0)

    return call, calls


@pytest.fixture(autouse=True)
def no_sleep():
    with patch("max_bridge.qr_login.asyncio.sleep", new=AsyncMock()):
        yield


class TestRunQrFlow:
    @pytest.mark.asyncio
    async def test_returns_token_after_approval(self):
        call, calls = scripted(
            (1, qr(1)),
            (1, {"status": {"ttl": 100_000}}),
            (1, {"status": {"loginAvailable": True}}),
            (1, {"tokenAttrs": {"LOGIN": {"token": "own_token"}}}),
        )
        on_qr = AsyncMock()

        token = await run_qr_flow(call, on_qr)

        assert token == "own_token"
        on_qr.assert_awaited_once_with("https://max.ru/:auth/1")
        assert calls[-1] == (OpCode.LOGIN_BY_QR, {"trackId": "track1"})

    @pytest.mark.asyncio
    async def test_expired_qr_is_replaced(self):
        call, _ = scripted(
            (1, qr(1, ttl=0)),
            (1, qr(2)),
            (1, {"status": {"loginAvailable": True}}),
            (1, {"tokenAttrs": {"LOGIN": {"token": "tok"}}}),
        )
        on_qr = AsyncMock()

        assert await run_qr_flow(call, on_qr) == "tok"
        assert [c.args[0] for c in on_qr.await_args_list] == [
            "https://max.ru/:auth/1", "https://max.ru/:auth/2",
        ]

    @pytest.mark.asyncio
    async def test_track_dropped_at_expiry_gets_new_qr(self):
        call, _ = scripted(
            (1, qr(1)),
            (3, {"error": "error", "message": "invalid trackId"}),
            (1, qr(2)),
            (1, {"status": {"loginAvailable": True}}),
            (1, {"tokenAttrs": {"LOGIN": {"token": "tok"}}}),
        )
        on_qr = AsyncMock()

        assert await run_qr_flow(call, on_qr) == "tok"
        assert on_qr.await_count == 2

    @pytest.mark.asyncio
    async def test_gives_up_after_attempts(self):
        call, _ = scripted((1, qr(1, ttl=0)), (1, qr(2, ttl=0)))

        with pytest.raises(QrExpiredError):
            await run_qr_flow(call, AsyncMock(), attempts=2)

    @pytest.mark.asyncio
    async def test_qr_disabled_is_reported(self):
        call, _ = scripted((3, {"error": "qr_login.disabled", "message": "qr login disabled"}))

        with pytest.raises(QrLoginError, match="qr login disabled"):
            await run_qr_flow(call, AsyncMock())

    @pytest.mark.asyncio
    async def test_password_challenge_is_reported(self):
        call, _ = scripted(
            (1, qr(1)),
            (1, {"status": {"loginAvailable": True}}),
            (1, {"passwordChallenge": {"trackId": "p", "hint": "h"}}),
        )

        with pytest.raises(QrLoginError, match="2FA"):
            await run_qr_flow(call, AsyncMock())
