"""
helius.py -- Helius RPC: smart transaction sending + confirmation polling.
Flat layout -- no sub-package imports.
"""

import asyncio
import logging
import aiohttp
from typing import Optional

from helpers import async_retry

logger = logging.getLogger(__name__)


class HeliusClient:
    def __init__(self, api_key: str, session: aiohttp.ClientSession):
        self.api_key  = api_key
        self.session  = session
        self.base_url = f"https://mainnet.helius-rpc.com/?api-key={api_key}"

    @async_retry(max_attempts=4, delay=0.5, backoff=2.0)
    async def send_smart_transaction(self, encoded_tx: str) -> str:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method":  "sendTransaction",
            "params":  [encoded_tx, {
                "encoding":            "base64",
                "skipPreflight":       True,
                "preflightCommitment": "processed",
                "maxRetries":          5,
            }],
        }
        async with self.session.post(
            self.base_url, json=payload, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if "error" in data:
            raise RuntimeError(f"Helius sendTransaction error: {data['error']}")
        sig = data.get("result")
        logger.info(f"[Helius] Tx sent: {sig}")
        return sig

    @async_retry(max_attempts=5, delay=2.0, backoff=1.5)
    async def get_transaction_status(self, signature: str) -> Optional[str]:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method":  "getSignatureStatuses",
            "params":  [[signature], {"searchTransactionHistory": True}],
        }
        async with self.session.post(
            self.base_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        statuses = (data.get("result") or {}).get("value", [None])
        status   = statuses[0] if statuses else None
        if status is None:
            return None
        if status.get("err"):
            raise RuntimeError(f"Tx failed: {status['err']}")
        return status.get("confirmationStatus")

    async def wait_for_confirmation(
        self, signature: str, max_polls: int = 20, poll_interval: float = 2.0
    ) -> bool:
        for _ in range(max_polls):
            status = await self.get_transaction_status(signature)
            if status in ("confirmed", "finalized"):
                logger.info(f"[Helius] Confirmed: {signature}")
                return True
            await asyncio.sleep(poll_interval)
        logger.warning(f"[Helius] Not confirmed after {max_polls} polls: {signature}")
        return False
