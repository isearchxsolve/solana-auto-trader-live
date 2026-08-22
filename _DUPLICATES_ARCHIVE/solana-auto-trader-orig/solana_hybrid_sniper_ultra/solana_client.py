"""
solana_client.py -- Low-level Solana RPC wrapper using solders.
Flat layout -- no sub-package imports.
"""

import logging
import aiohttp
from typing import Any

from solders.keypair import Keypair  # type: ignore
import base58

from helpers import async_retry

logger = logging.getLogger(__name__)


def load_keypair(private_key_b58: str) -> Keypair:
    raw = base58.b58decode(private_key_b58)
    return Keypair.from_bytes(raw)


class SolanaRpcClient:
    def __init__(self, rpc_url: str, session: aiohttp.ClientSession):
        self.rpc_url = rpc_url
        self.session = session
        self._id     = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _call(self, method: str, params: list) -> Any:
        payload = {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params}
        async with self.session.post(
            self.rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error [{method}]: {data['error']}")
        return data.get("result")

    @async_retry(max_attempts=3, delay=1.0)
    async def get_balance(self, pubkey: str) -> float:
        result = await self._call("getBalance", [pubkey, {"commitment": "confirmed"}])
        return result.get("value", 0) / 1_000_000_000

    @async_retry(max_attempts=3, delay=1.0)
    async def get_latest_blockhash(self) -> str:
        result = await self._call("getLatestBlockhash", [{"commitment": "finalized"}])
        return result["value"]["blockhash"]

    @async_retry(max_attempts=3, delay=2.0, backoff=2.0)
    async def send_transaction(self, encoded_tx: str, skip_preflight: bool = False) -> str:
        return await self._call(
            "sendTransaction",
            [encoded_tx, {"encoding": "base64", "skipPreflight": skip_preflight,
                          "preflightCommitment": "processed", "maxRetries": 3}],
        )

    @async_retry(max_attempts=5, delay=2.0, backoff=1.5)
    async def confirm_transaction(self, signature: str) -> bool:
        result = await self._call(
            "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}]
        )
        statuses = (result or {}).get("value", [None])
        status   = statuses[0] if statuses else None
        if status is None:
            return False
        if status.get("err"):
            raise RuntimeError(f"Transaction failed: {status['err']}")
        return status.get("confirmationStatus") in ("confirmed", "finalized")
