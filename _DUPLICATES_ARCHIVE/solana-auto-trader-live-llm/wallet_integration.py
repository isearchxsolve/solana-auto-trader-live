#!/usr/bin/env python3
"""
Live Trading Wallet Integration for Solana.
Handles private key management, Jupiter swap execution,
transaction signing, and on-chain broadcasting.

Supports:
  - base58 private key (Phantom / Solflare export)
  - JSON array private key  [1,2,3,...,64]
  - Environment variable    SOLANA_PRIVATE_KEY
  - Config file             .wallet_config.json
"""

import os
import json
import base64
import time
from typing import Optional, Dict
from dataclasses import dataclass

import requests


@dataclass
class WalletConfig:
    """Wallet configuration for live trading."""
    private_key:            str
    rpc_url:                str = "https://api.mainnet-beta.solana.com"
    max_slippage_bps:       int = 100    # 1%
    priority_fee_lamports:  int = 5000   # micro-lamports — raised for reliable inclusion


class SolanaWallet:
    """
    Solana wallet for live Jupiter swaps.

    Requires:
        pip install solana solders base58
    """

    JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"

    def __init__(self, config: WalletConfig):
        self.config = config
        self._load_libs()
        self._load_keypair()
        self.rpc = self.Client(config.rpc_url)
        print(f"✓ Wallet loaded: {self.public_key}")

    # ── Initialisation ────────────────────────────────────────────────────────

    def _load_libs(self):
        """Import solana / solders libraries. Raises ImportError with instructions."""
        try:
            from solders.keypair    import Keypair
            from solders.pubkey     import Pubkey
            from solders.signature  import Signature
            from solders.transaction import VersionedTransaction
            from solana.rpc.api     import Client
            from solana.rpc.types   import TxOpts

            self.Keypair             = Keypair
            self.Pubkey              = Pubkey
            self.Signature           = Signature
            self.VersionedTransaction = VersionedTransaction
            self.Client              = Client
            self.TxOpts              = TxOpts
        except ImportError:
            raise ImportError(
                "Live trading requires:\n"
                "  pip install solana solders base58\n"
            )

    def _load_keypair(self):
        """Decode private key (base58 or JSON array) into a Keypair."""
        try:
            import base58
            key_str = self.config.private_key.strip()

            # JSON array: [1, 2, 3, ..., 64]
            if key_str.startswith("["):
                key_bytes = bytes(json.loads(key_str))
            else:
                key_bytes = base58.b58decode(key_str)

            if len(key_bytes) not in (32, 64):
                raise ValueError(
                    f"Private key is {len(key_bytes)} bytes; expected 32 or 64."
                )

            self.keypair    = self.Keypair.from_bytes(key_bytes)
            self.public_key = str(self.keypair.pubkey())

        except ImportError:
            raise ImportError("Install base58:  pip install base58")
        except Exception as e:
            raise ValueError(f"Failed to load keypair: {e}")

    # ── Balance ───────────────────────────────────────────────────────────────

    def get_balance(self, token_mint: Optional[str] = None) -> float:
        """
        SOL balance (token_mint=None) or SPL token balance.
        Returns float in human units.
        """
        try:
            if token_mint is None:
                resp = self.rpc.get_balance(self.keypair.pubkey())
                return resp.value / 1e9
            # SPL balance via getTokenAccountsByOwner
            resp = self.rpc.get_token_accounts_by_owner_json_parsed(
                self.keypair.pubkey(),
                {"mint": token_mint},
            )
            accounts = resp.value
            if not accounts:
                return 0.0
            amount_str = (accounts[0].account.data.parsed
                          ["info"]["tokenAmount"]["uiAmountString"])
            return float(amount_str)
        except Exception as e:
            print(f"  [wallet] get_balance error: {e}")
            return 0.0

    # ── Swap execution ────────────────────────────────────────────────────────

    def execute_jupiter_swap(
        self,
        quote:       Dict,
        max_retries: int = 3,
    ) -> Optional[str]:
        """
        Execute a Jupiter v6 swap end-to-end.

        1. POST to Jupiter /swap to get the serialised transaction
        2. Deserialise as VersionedTransaction
        3. Sign with self.keypair
        4. Send via RPC
        5. Wait for confirmation

        Returns the transaction signature string on success, None on failure.
        """
        for attempt in range(1, max_retries + 1):
            try:
                # ── Step 1: get swap transaction bytes from Jupiter ────────────
                swap_payload = {
                    "quoteResponse":             quote,
                    "userPublicKey":             self.public_key,
                    "wrapAndUnwrapSol":          True,
                    "dynamicComputeUnitLimit":   True,
                    "prioritizationFeeLamports": self.config.priority_fee_lamports,
                }
                resp = requests.post(
                    self.JUPITER_SWAP_URL,
                    json=swap_payload,
                    timeout=30,
                )
                if resp.status_code != 200:
                    print(f"  [swap] Jupiter /swap HTTP {resp.status_code}: "
                          f"{resp.text[:300]}")
                    if attempt < max_retries:
                        time.sleep(2 ** attempt)
                        continue
                    return None

                swap_data = resp.json()
                tx_b64    = swap_data.get("swapTransaction", "")
                if not tx_b64:
                    print("  [swap] No swapTransaction in Jupiter response")
                    return None

                # ── Step 2: deserialise ────────────────────────────────────────
                tx_bytes    = base64.b64decode(tx_b64)
                transaction = self.VersionedTransaction.from_bytes(tx_bytes)

                # ── Step 3: sign ───────────────────────────────────────────────
                # VersionedTransaction.sign() takes a list of Keypair objects.
                # Do NOT use keypair.sign_message() — that returns a raw Signature,
                # not a Keypair, and will raise a type error.
                transaction.sign([self.keypair])

                # ── Step 4: send ───────────────────────────────────────────────
                print(f"  [swap] Sending transaction (attempt {attempt}/{max_retries}) ...")
                result = self.rpc.send_raw_transaction(
                    bytes(transaction),
                    opts=self.TxOpts(skip_preflight=False, max_retries=3),
                )

                if not result.value:
                    print("  [swap] RPC returned no signature")
                    if attempt < max_retries:
                        time.sleep(2 ** attempt)
                        continue
                    return None

                tx_sig = str(result.value)
                print(f"  [swap] Sent: {tx_sig}")

                # ── Step 5: wait for confirmation ──────────────────────────────
                if self._wait_confirmation(tx_sig):
                    print(f"  [swap] ✓ Confirmed: {tx_sig}")
                    return tx_sig
                else:
                    print(f"  [swap] Not confirmed (attempt {attempt})")
                    if attempt < max_retries:
                        time.sleep(2 ** attempt)
                        continue
                    return None

            except Exception as e:
                print(f"  [swap] Error (attempt {attempt}): {e}")
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                else:
                    return None

        return None

    # ── Confirmation polling ──────────────────────────────────────────────────

    def _wait_confirmation(self, tx_sig: str, max_wait: int = 90) -> bool:
        """
        Poll RPC for transaction status.
        Returns True once the transaction is confirmed or finalised,
        False if it errors or times out.
        """
        sig      = self.Signature.from_string(tx_sig)
        deadline = time.time() + max_wait

        while time.time() < deadline:
            try:
                resp = self.rpc.get_signature_statuses([sig])
                if resp.value and resp.value[0]:
                    status = resp.value[0]
                    if status.err:
                        print(f"  [swap] Transaction failed on-chain: {status.err}")
                        return False
                    if status.confirmation_status in ("confirmed", "finalized"):
                        return True
            except Exception as e:
                print(f"  [swap] Status poll error: {e}")
            time.sleep(2)

        print(f"  [swap] Confirmation timeout after {max_wait}s")
        return False


# ── Config loading ─────────────────────────────────────────────────────────────

def load_wallet_config(config_path: str = ".wallet_config.json") -> Optional[WalletConfig]:
    """
    Load wallet configuration.

    Priority:
      1. Environment variables  SOLANA_PRIVATE_KEY  (+ optionally SOLANA_RPC_URL)
      2. Config file            .wallet_config.json
    """
    # 1. Environment variables
    pk = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if pk:
        print("✓ Wallet config loaded from environment variables")
        return WalletConfig(
            private_key=pk,
            rpc_url=os.getenv("SOLANA_RPC_URL",
                              "https://api.mainnet-beta.solana.com"),
        )

    # 2. Config file
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                data = json.load(f)
            print(f"✓ Wallet config loaded from {config_path}")
            return WalletConfig(
                private_key=data["private_key"],
                rpc_url=data.get("rpc_url",
                                 "https://api.mainnet-beta.solana.com"),
                max_slippage_bps=data.get("max_slippage_bps",      100),
                priority_fee_lamports=data.get("priority_fee_lamports", 5000),
            )
        except Exception as e:
            print(f"✗ Error reading {config_path}: {e}")
            return None

    print("✗ No wallet configuration found.")
    print("  Option 1 (recommended): set environment variable")
    print("    export SOLANA_PRIVATE_KEY='<base58_or_json_array_key>'")
    print("  Option 2: run  python setup_wallet.py --setup")
    return None


def create_example_config():
    """Write a commented example .wallet_config.json."""
    example = {
        "_comment":              "NEVER commit this file to git!",
        "private_key":           "YOUR_BASE58_OR_JSON_ARRAY_PRIVATE_KEY_HERE",
        "rpc_url":               "https://api.mainnet-beta.solana.com",
        "max_slippage_bps":      100,
        "priority_fee_lamports": 5000,
    }
    with open(".wallet_config.example.json", "w") as f:
        json.dump(example, f, indent=2)
    print("✓ Created .wallet_config.example.json")
    print("  Copy to .wallet_config.json and fill in your private key.")


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Solana wallet integration test")
    parser.add_argument("--create-example", action="store_true",
                        help="Create example config file")
    parser.add_argument("--check-balance",  action="store_true",
                        help="Check wallet SOL balance")
    args = parser.parse_args()

    if args.create_example:
        create_example_config()
        raise SystemExit(0)

    cfg = load_wallet_config()
    if not cfg:
        raise SystemExit(1)

    try:
        wallet = SolanaWallet(cfg)
        if args.check_balance:
            sol = wallet.get_balance()
            print(f"\n💰 SOL Balance: {sol:.4f} SOL")
            if sol < 0.01:
                print("⚠️  Low SOL — top up before live trading.")
        else:
            print("\n✓ Wallet integration working.")
            print("  Run with --check-balance to verify your balance.")
    except Exception as e:
        print(f"✗ Error: {e}")
        raise SystemExit(1)
