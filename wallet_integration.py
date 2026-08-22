#!/usr/bin/env python3
"""
Live Trading Wallet Integration for Solana
Handles private key management, transaction signing, and swap execution
"""

import os
import json
import base64
import base58
import requests
import time
from typing import Optional, Dict
from dataclasses import dataclass


@dataclass
class WalletConfig:
    """Wallet configuration for live trading"""
    private_key: str
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    max_slippage_bps: int = 100  # 1% default slippage
    priority_fee_lamports: int = 1000  # Micro-lamports for priority


class SolanaWallet:
    """Solana wallet integration for live trading"""
    
    def __init__(self, config: WalletConfig):
        """
        Initialize wallet with configuration
        
        Args:
            config: WalletConfig with private key and settings
        """
        self.config = config
        self.keypair = None
        self.public_key = None
        
        # Import required libraries
        try:
            from solders.keypair import Keypair
            from solders.pubkey import Pubkey
            from solana.rpc.api import Client
            from solana.transaction import Transaction
            from solders.transaction import VersionedTransaction
            from solders.message import MessageV0
            
            self.Keypair = Keypair
            self.Pubkey = Pubkey
            self.Client = Client
            self.Transaction = Transaction
            self.VersionedTransaction = VersionedTransaction
            self.MessageV0 = MessageV0
            
        except ImportError:
            raise ImportError(
                "Live trading requires additional dependencies:\n"
                "  pip install solana solders base58\n"
                "Please install and try again."
            )
        
        # Initialize wallet
        self._load_keypair()
        self.rpc_client = self.Client(self.config.rpc_url)
        
        print(f"✓ Wallet loaded: {self.public_key}")
    
    def _load_keypair(self):
        """Load keypair from private key"""
        try:
            # Decode base58 private key
            private_key_bytes = base58.b58decode(self.config.private_key)
            
            # Create keypair
            self.keypair = self.Keypair.from_bytes(private_key_bytes)
            self.public_key = str(self.keypair.pubkey())
            
        except Exception as e:
            raise ValueError(f"Failed to load private key: {e}")
    
    def get_balance(self, token_mint: Optional[str] = None) -> float:
        """
        Get wallet balance
        
        Args:
            token_mint: Token mint address (None for SOL)
            
        Returns:
            Balance in token units
        """
        try:
            if token_mint is None:
                # Get SOL balance
                response = self.rpc_client.get_balance(self.keypair.pubkey())
                return response.value / 1e9  # Convert lamports to SOL
            else:
                # Get SPL token balance
                from solders.pubkey import Pubkey
                
                # This would require token account lookup
                # For now, return 0 - implement if needed
                return 0.0
                
        except Exception as e:
            print(f"Error getting balance: {e}")
            return 0.0
    
    def execute_jupiter_swap(
        self, 
        quote: Dict,
        max_retries: int = 3
    ) -> Optional[str]:
        """
        Execute a Jupiter swap transaction
        
        Args:
            quote: Jupiter quote response
            max_retries: Maximum number of retry attempts
            
        Returns:
            Transaction signature if successful, None otherwise
        """
        
        for attempt in range(max_retries):
            try:
                # Step 1: Get swap transaction from Jupiter
                swap_response = requests.post(
                    "https://quote-api.jup.ag/v6/swap",
                    json={
                        "quoteResponse": quote,
                        "userPublicKey": self.public_key,
                        "wrapAndUnwrapSol": True,
                        "dynamicComputeUnitLimit": True,
                        "prioritizationFeeLamports": self.config.priority_fee_lamports
                    },
                    timeout=30
                )
                
                if swap_response.status_code != 200:
                    print(f"❌ Swap instruction failed: {swap_response.text}")
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)  # Exponential backoff
                        continue
                    return None
                
                swap_data = swap_response.json()
                
                # Step 2: Deserialize the transaction
                tx_bytes = base64.b64decode(swap_data['swapTransaction'])
                
                # Try to deserialize as VersionedTransaction
                try:
                    transaction = self.VersionedTransaction.from_bytes(tx_bytes)
                except Exception:
                    # Fallback: might be legacy transaction
                    print("⚠️  Warning: Using legacy transaction format")
                    from solana.transaction import Transaction as LegacyTransaction
                    transaction = LegacyTransaction.deserialize(tx_bytes)
                
                # Step 3: Sign the transaction
                # For VersionedTransaction
                if hasattr(transaction, 'message'):
                    # Create new signed transaction
                    signed_tx = self.VersionedTransaction(
                        transaction.message,
                        [self.keypair.sign_message(bytes(transaction.message))]
                    )
                    tx_payload = bytes(signed_tx)
                else:
                    # Legacy transaction
                    transaction.sign(self.keypair)
                    tx_payload = transaction.serialize()
                
                # Step 4: Send to Solana network
                print(f"📤 Sending transaction (attempt {attempt + 1}/{max_retries})...")
                
                result = self.rpc_client.send_raw_transaction(
                    tx_payload,
                    opts={"skipPreflight": False, "maxRetries": 3}
                )
                
                if result.value:
                    tx_sig = str(result.value)
                    print(f"✓ Transaction sent: {tx_sig}")
                    
                    # Wait for confirmation
                    confirmed = self._wait_for_confirmation(tx_sig)
                    if confirmed:
                        print(f"✓ Transaction confirmed!")
                        return tx_sig
                    else:
                        print(f"⚠️  Transaction not confirmed, may have failed")
                        if attempt < max_retries - 1:
                            continue
                        return None
                else:
                    print(f"❌ Transaction submission failed")
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    return None
                    
            except Exception as e:
                print(f"❌ Error executing swap (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff
                    continue
                return None
        
        return None
    
    def _wait_for_confirmation(
        self, 
        tx_signature: str, 
        max_wait: int = 60
    ) -> bool:
        """
        Wait for transaction confirmation
        
        Args:
            tx_signature: Transaction signature to check
            max_wait: Maximum seconds to wait
            
        Returns:
            True if confirmed, False otherwise
        """
        from solders.signature import Signature
        
        start_time = time.time()
        sig = Signature.from_string(tx_signature)
        
        while time.time() - start_time < max_wait:
            try:
                response = self.rpc_client.get_signature_statuses([sig])
                if response.value and response.value[0]:
                    status = response.value[0]
                    
                    if status.confirmation_status:
                        # Transaction is at least confirmed
                        if status.err:
                            print(f"⚠️  Transaction failed: {status.err}")
                            return False
                        return True
                
                time.sleep(2)
                
            except Exception as e:
                print(f"⚠️  Error checking confirmation: {e}")
                time.sleep(2)
        
        print(f"⚠️  Confirmation timeout after {max_wait}s")
        return False


def load_wallet_config(config_path: str = ".wallet_config.json") -> Optional[WalletConfig]:
    """
    Load wallet configuration from file or environment
    
    Priority:
    1. Environment variables (SOLANA_PRIVATE_KEY, SOLANA_RPC_URL)
    2. Config file (.wallet_config.json)
    
    Args:
        config_path: Path to config file
        
    Returns:
        WalletConfig if successful, None otherwise
    """
    
    # Try environment variables first (more secure)
    private_key = os.getenv('SOLANA_PRIVATE_KEY')
    rpc_url = os.getenv('SOLANA_RPC_URL', 'https://api.mainnet-beta.solana.com')
    
    if private_key:
        print("✓ Loaded wallet config from environment variables")
        return WalletConfig(
            private_key=private_key,
            rpc_url=rpc_url
        )
    
    # Try config file
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config_data = json.load(f)
            
            print(f"✓ Loaded wallet config from {config_path}")
            return WalletConfig(
                private_key=config_data['private_key'],
                rpc_url=config_data.get('rpc_url', 'https://api.mainnet-beta.solana.com'),
                max_slippage_bps=config_data.get('max_slippage_bps', 100),
                priority_fee_lamports=config_data.get('priority_fee_lamports', 1000)
            )
            
        except Exception as e:
            print(f"❌ Error loading config file: {e}")
            return None
    
    print("❌ No wallet configuration found!")
    print("\nPlease set up your wallet using one of these methods:")
    print("\n1. Environment variables (RECOMMENDED):")
    print("   export SOLANA_PRIVATE_KEY='your_base58_private_key'")
    print("   export SOLANA_RPC_URL='https://api.mainnet-beta.solana.com'")
    print("\n2. Config file (.wallet_config.json):")
    print('   {"private_key": "your_base58_private_key", "rpc_url": "..."}')
    print("\n⚠️  NEVER commit .wallet_config.json to git!")
    
    return None


def create_example_config():
    """Create an example wallet config file"""
    example = {
        "private_key": "YOUR_BASE58_PRIVATE_KEY_HERE",
        "rpc_url": "https://api.mainnet-beta.solana.com",
        "max_slippage_bps": 100,
        "priority_fee_lamports": 1000,
        "_comment": "NEVER commit this file to git! Add to .gitignore"
    }
    
    with open('.wallet_config.example.json', 'w') as f:
        json.dump(example, f, indent=2)
    
    print("✓ Created .wallet_config.example.json")
    print("  Copy to .wallet_config.json and add your private key")


if __name__ == "__main__":
    """Test wallet integration"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Test Solana wallet integration")
    parser.add_argument("--create-example", action="store_true",
                       help="Create example config file")
    parser.add_argument("--check-balance", action="store_true",
                       help="Check wallet balance")
    
    args = parser.parse_args()
    
    if args.create_example:
        create_example_config()
        exit(0)
    
    # Load wallet
    config = load_wallet_config()
    if not config:
        print("\n❌ Failed to load wallet configuration")
        exit(1)
    
    try:
        wallet = SolanaWallet(config)
        
        if args.check_balance:
            sol_balance = wallet.get_balance()
            print(f"\n💰 SOL Balance: {sol_balance:.4f} SOL")
            
            if sol_balance < 0.01:
                print("⚠️  Warning: Low SOL balance for gas fees!")
        else:
            print("\n✓ Wallet integration working!")
            print("  Use --check-balance to check your balance")
            
    except Exception as e:
        print(f"\n❌ Error: {e}")
        exit(1)
