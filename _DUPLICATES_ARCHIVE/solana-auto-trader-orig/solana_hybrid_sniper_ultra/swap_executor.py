"""
swap_executor.py -- Jupiter v6 quote + swap execution with solders signing.
Flat layout -- no sub-package imports.
"""

import os
import base64
import logging
import aiohttp
from typing import Optional, Dict

from solders.keypair     import Keypair           # type: ignore
from solders.transaction import VersionedTransaction  # type: ignore

from helpers        import async_retry, sol_to_lamports
from helius         import HeliusClient
from solana_client  import SolanaRpcClient

logger = logging.getLogger(__name__)

JUPITER_BASE = os.getenv("JUPITER_BASE_URL", "https://quote-api.jup.ag/v6")
BASE_TOKEN   = os.getenv("BASE_TOKEN", "So11111111111111111111111111111111111111112")


class SwapExecutor:
    def __init__(
        self,
        keypair:      Keypair,
        rpc:          SolanaRpcClient,
        helius:       HeliusClient,
        session:      aiohttp.ClientSession,
        slippage_bps: int = 300,
    ):
        self.keypair      = keypair
        self.rpc          = rpc
        self.helius       = helius
        self.session      = session
        self.slippage_bps = slippage_bps
        self.pubkey       = str(keypair.pubkey())

    @async_retry(max_attempts=3, delay=0.5)
    async def get_quote(
        self,
        input_mint:      str,
        output_mint:     str,
        amount_lamports: int,
        slippage_bps:    Optional[int] = None,
    ) -> Dict:
        slip   = slippage_bps if slippage_bps is not None else self.slippage_bps
        params = {
            "inputMint":   input_mint,
            "outputMint":  output_mint,
            "amount":      str(amount_lamports),
            "slippageBps": slip,
            "onlyDirectRoutes": "false",
        }
        async with self.session.get(
            f"{JUPITER_BASE}/quote", params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    @async_retry(max_attempts=3, delay=0.5)
    async def get_swap_transaction(self, quote: Dict) -> str:
        payload = {
            "quoteResponse":                 quote,
            "userPublicKey":                 self.pubkey,
            "wrapAndUnwrapSol":              True,
            "computeUnitPriceMicroLamports": 100_000,
            "asLegacyTransaction":           False,
        }
        async with self.session.post(
            f"{JUPITER_BASE}/swap", json=payload, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data["swapTransaction"]

    def sign_transaction(self, swap_tx_b64: str) -> str:
        raw    = base64.b64decode(swap_tx_b64)
        tx     = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(tx.message, [self.keypair])
        return base64.b64encode(bytes(signed)).decode()

    async def execute_swap(
        self,
        input_mint:   str,
        output_mint:  str,
        amount_sol:   float,
        slippage_bps: Optional[int] = None,
    ) -> Optional[str]:
        try:
            amount_lamports = sol_to_lamports(amount_sol)
            logger.info(f"[Swap] {amount_sol} SOL | {input_mint[:8]}.. -> {output_mint[:8]}..")
            quote       = await self.get_quote(input_mint, output_mint, amount_lamports, slippage_bps)
            swap_tx_b64 = await self.get_swap_transaction(quote)
            signed_b64  = self.sign_transaction(swap_tx_b64)
            sig         = await self.helius.send_smart_transaction(signed_b64)
            confirmed   = await self.helius.wait_for_confirmation(sig)
            if confirmed:
                logger.info(f"[Swap] Confirmed: {sig}")
            else:
                logger.warning(f"[Swap] Unconfirmed: {sig}")
            return sig
        except Exception as exc:
            logger.error(f"[Swap] Failed: {exc}", exc_info=True)
            return None

    async def buy(self, token_mint: str, amount_sol: float, slippage_bps: Optional[int] = None) -> Optional[str]:
        return await self.execute_swap(BASE_TOKEN, token_mint, amount_sol, slippage_bps)

    async def sell(self, token_mint: str, amount_sol: float, slippage_bps: Optional[int] = None) -> Optional[str]:
        return await self.execute_swap(token_mint, BASE_TOKEN, amount_sol, slippage_bps)
