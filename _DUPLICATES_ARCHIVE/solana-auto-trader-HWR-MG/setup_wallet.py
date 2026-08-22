#!/usr/bin/env python3
"""
Wallet Setup Helper
Converts wallet private keys to base58 format needed by the trading bot
"""

import json
import sys


def convert_to_base58(private_key_input: str) -> str:
    """
    Convert various private key formats to base58
    
    Supports:
    - JSON array: [1,2,3,4,...] (Phantom/Solflare export)
    - Hex string: 0x1234abcd...
    - Base58 string: already in correct format
    """
    try:
        import base58
    except ImportError:
        print("❌ Error: base58 library not installed")
        print("   Install with: pip install base58")
        sys.exit(1)
    
    # Remove whitespace
    private_key_input = private_key_input.strip()
    
    # Case 1: JSON array format [1,2,3,4,...]
    if private_key_input.startswith('['):
        try:
            # Parse JSON array
            key_array = json.loads(private_key_input)
            
            # Convert to bytes
            key_bytes = bytes(key_array)
            
            # Validate length (should be 64 bytes for Solana)
            if len(key_bytes) != 64:
                print(f"⚠️  Warning: Key length is {len(key_bytes)} bytes (expected 64)")
            
            # Convert to base58
            base58_key = base58.b58encode(key_bytes).decode('ascii')
            return base58_key
            
        except json.JSONDecodeError as e:
            print(f"❌ Error parsing JSON array: {e}")
            return None
    
    # Case 2: Hex format (0x... or just hex string)
    elif private_key_input.startswith('0x') or all(c in '0123456789abcdefABCDEF' for c in private_key_input):
        try:
            # Remove 0x prefix if present
            hex_string = private_key_input[2:] if private_key_input.startswith('0x') else private_key_input
            
            # Convert hex to bytes
            key_bytes = bytes.fromhex(hex_string)
            
            # Validate length
            if len(key_bytes) != 64:
                print(f"⚠️  Warning: Key length is {len(key_bytes)} bytes (expected 64)")
            
            # Convert to base58
            base58_key = base58.b58encode(key_bytes).decode('ascii')
            return base58_key
            
        except ValueError as e:
            print(f"❌ Error parsing hex string: {e}")
            return None
    
    # Case 3: Already base58 format
    else:
        try:
            # Try to decode to verify it's valid base58
            key_bytes = base58.b58decode(private_key_input)
            
            # Validate length
            if len(key_bytes) != 64:
                print(f"⚠️  Warning: Key length is {len(key_bytes)} bytes (expected 64)")
            
            print("✓ Private key is already in base58 format")
            return private_key_input
            
        except Exception as e:
            print(f"❌ Error: Invalid base58 string: {e}")
            return None


def setup_wallet_config():
    """Interactive wallet configuration setup"""
    print("="*70)
    print("  SOLANA TRADING BOT - WALLET SETUP")
    print("="*70)
    print("\nThis tool will help you set up your wallet for live trading.")
    print("\n⚠️  SECURITY WARNING:")
    print("  - Your private key will be stored in .wallet_config.json")
    print("  - NEVER share this file or commit it to git")
    print("  - Keep backups in a secure location")
    print("="*70)
    
    # Get private key
    print("\n📋 STEP 1: Export your private key from your wallet")
    print("\nFor Phantom wallet:")
    print("  Settings → Security & Privacy → Export Private Key")
    print("\nFor Solflare wallet:")
    print("  Settings → Reveal Private Key")
    print("\nYour private key may look like:")
    print("  - JSON array: [1,2,3,4,...]")
    print("  - Hex string: 0x1234abcd...")
    print("  - Base58 string: 5J3mBbAH58CpQ3Y2bnYy...")
    
    print("\n" + "="*70)
    private_key_input = input("\nPaste your private key here: ").strip()
    
    if not private_key_input:
        print("❌ No private key provided")
        return False
    
    # Convert to base58
    print("\n🔄 Converting to base58 format...")
    base58_key = convert_to_base58(private_key_input)
    
    if not base58_key:
        print("\n❌ Failed to convert private key")
        return False
    
    print(f"✓ Converted successfully!")
    print(f"  Base58 key (first 10 chars): {base58_key[:10]}...")
    
    # Get RPC URL
    print("\n" + "="*70)
    print("📋 STEP 2: Choose RPC endpoint")
    print("\nOptions:")
    print("  1. Free Solana RPC (slower, rate limited)")
    print("  2. Custom RPC URL (Helius, QuickNode, Alchemy)")
    
    rpc_choice = input("\nEnter choice (1 or 2) [default: 1]: ").strip()
    
    if rpc_choice == "2":
        rpc_url = input("Enter your RPC URL: ").strip()
        if not rpc_url.startswith('http'):
            print("⚠️  Invalid RPC URL, using default")
            rpc_url = "https://api.mainnet-beta.solana.com"
    else:
        rpc_url = "https://api.mainnet-beta.solana.com"
    
    # Slippage
    print("\n" + "="*70)
    print("📋 STEP 3: Set maximum slippage")
    slippage_input = input("Max slippage in % [default: 1%]: ").strip()
    
    try:
        slippage_pct = float(slippage_input) if slippage_input else 1.0
        slippage_bps = int(slippage_pct * 100)
    except:
        slippage_bps = 100
    
    # Priority fee
    print("\n" + "="*70)
    print("📋 STEP 4: Set priority fee")
    print("Higher fees = faster execution (recommended for meme coins)")
    priority_input = input("Priority fee in micro-lamports [default: 1000]: ").strip()
    
    try:
        priority_fee = int(priority_input) if priority_input else 1000
    except:
        priority_fee = 1000
    
    # Create config
    config = {
        "private_key": base58_key,
        "rpc_url": rpc_url,
        "max_slippage_bps": slippage_bps,
        "priority_fee_lamports": priority_fee
    }
    
    # Save config
    print("\n" + "="*70)
    print("💾 Saving configuration...")
    
    try:
        with open('.wallet_config.json', 'w') as f:
            json.dump(config, f, indent=2)
        
        print("✓ Configuration saved to .wallet_config.json")
        
        # Verify it's in .gitignore
        print("\n🔒 Security check...")
        try:
            with open('.gitignore', 'r') as f:
                gitignore_content = f.read()
                if '.wallet_config.json' in gitignore_content:
                    print("✓ .wallet_config.json is in .gitignore")
                else:
                    print("⚠️  WARNING: .wallet_config.json is NOT in .gitignore!")
                    print("   Add it to .gitignore to prevent accidental commits!")
        except FileNotFoundError:
            print("⚠️  .gitignore file not found")
            print("   Create one and add: .wallet_config.json")
        
        # Test the wallet
        print("\n" + "="*70)
        test_choice = input("\n🧪 Test wallet connection? (yes/no) [default: yes]: ").strip().lower()
        
        if test_choice in ['', 'yes', 'y']:
            print("\n🔄 Testing wallet connection...")
            try:
                from wallet_integration import load_wallet_config, SolanaWallet
                
                config_obj = load_wallet_config()
                wallet = SolanaWallet(config_obj)
                
                sol_balance = wallet.get_balance()
                print(f"\n✓ Wallet connection successful!")
                print(f"  Address: {wallet.public_key}")
                print(f"  SOL Balance: {sol_balance:.4f} SOL")
                
                if sol_balance < 0.01:
                    print("\n⚠️  WARNING: Low SOL balance!")
                    print("   You need at least 0.01 SOL for gas fees")
                    print("   Send SOL to this address before trading")
                
            except Exception as e:
                print(f"\n❌ Wallet test failed: {e}")
                return False
        
        print("\n" + "="*70)
        print("✅ WALLET SETUP COMPLETE!")
        print("="*70)
        print("\nYou can now run live trading with:")
        print("  python solana_auto_trader_trending.py --mode live")
        print("\n⚠️  REMEMBER:")
        print("  - Start with small amounts ($50-100)")
        print("  - You can lose everything")
        print("  - Press Ctrl+C to stop at any time")
        print("="*70)
        
        return True
        
    except Exception as e:
        print(f"\n❌ Error saving configuration: {e}")
        return False


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Wallet setup helper")
    parser.add_argument("--convert", help="Convert a private key to base58")
    parser.add_argument("--setup", action="store_true", help="Interactive wallet setup")
    
    args = parser.parse_args()
    
    if args.convert:
        print("Converting private key to base58...")
        result = convert_to_base58(args.convert)
        if result:
            print(f"\nBase58 private key:")
            print(result)
            print(f"\n✓ Copy this key to your .wallet_config.json file")
        else:
            print("\n❌ Conversion failed")
            sys.exit(1)
    
    elif args.setup:
        success = setup_wallet_config()
        sys.exit(0 if success else 1)
    
    else:
        # Run interactive setup by default
        success = setup_wallet_config()
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
