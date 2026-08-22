# 🚀 LIVE TRADING SETUP GUIDE

Complete guide to set up and run your Solana meme coin trading bot in live mode.

---

## ⚠️ CRITICAL WARNING

**LIVE TRADING = REAL MONEY AT RISK**

- You can lose **ALL** your funds
- Meme coins are extremely volatile (can drop 99% instantly)
- Slippage on low-liquidity tokens can be 5-15%
- Start with **$50-100 maximum** until you verify it works
- Test thoroughly in paper mode first
- Never invest more than you can afford to lose completely

**The bot is working well in paper trading but that doesn't guarantee live profits!**

---

## 📋 PREREQUISITES

### 1. Install Live Trading Dependencies

```bash
pip install solana solders base58
```

### 2. Have a Funded Solana Wallet

You need:
- **Minimum 0.01 SOL** for gas fees (~$1-2)
- **USDC** to trade with (recommended: $50-100 for testing)
- A wallet with exported private key (Phantom, Solflare, etc.)

**IMPORTANT:** Use a dedicated trading wallet, NOT your main wallet!

---

## 🔧 SETUP PROCESS

### Method 1: Interactive Setup (RECOMMENDED)

Run the setup wizard:

```bash
python setup_wallet.py --setup
```

This will:
1. Guide you through exporting your private key
2. Convert it to the correct format
3. Set up your RPC endpoint
4. Configure slippage and fees
5. Test the wallet connection
6. Create `.wallet_config.json` file

### Method 2: Manual Setup

#### Step 2.1: Export Your Private Key

**From Phantom Wallet:**
1. Open Phantom
2. Click Settings → Security & Privacy
3. Click "Export Private Key"
4. Enter your password
5. Copy the private key

**From Solflare Wallet:**
1. Open Solflare
2. Click Settings
3. Click "Reveal Private Key"
4. Copy the private key

#### Step 2.2: Convert to Base58 Format

Your private key might be in different formats:

**Format 1: JSON Array** (most common)
```
[1,2,3,4,5,...]
```

**Format 2: Hex String**
```
0x1234abcdef...
```

**Format 3: Base58** (already correct)
```
5J3mBbAH58CpQ3Y2bnYy...
```

Convert using:
```bash
python setup_wallet.py --convert "[1,2,3,4,...]"
```

#### Step 2.3: Create Configuration File

Create `.wallet_config.json`:

```json
{
  "private_key": "YOUR_BASE58_PRIVATE_KEY_HERE",
  "rpc_url": "https://api.mainnet-beta.solana.com",
  "max_slippage_bps": 100,
  "priority_fee_lamports": 1000
}
```

**RPC Options:**
- Free (slower): `https://api.mainnet-beta.solana.com`
- Helius: `https://mainnet.helius-rpc.com/?api-key=YOUR_KEY`
- QuickNode: `https://YOUR_ENDPOINT.quiknode.pro/YOUR_KEY/`
- Alchemy: `https://solana-mainnet.g.alchemy.com/v2/YOUR_KEY`

**Slippage Settings:**
- `100` = 1% (conservative, may miss fast-moving trades)
- `200` = 2% (moderate, good for meme coins)
- `500` = 5% (aggressive, high cost but won't miss trades)

### Method 3: Environment Variables

More secure than config file:

```bash
export SOLANA_PRIVATE_KEY="your_base58_private_key_here"
export SOLANA_RPC_URL="https://api.mainnet-beta.solana.com"
```

Add to your `~/.bashrc` or `~/.zshrc` to persist across sessions.

---

## ✅ VERIFY SETUP

Test your wallet connection:

```bash
python wallet_integration.py --check-balance
```

Expected output:
```
✓ Loaded wallet config from environment variables
✓ Wallet loaded: AbC123...xyz789
💰 SOL Balance: 0.0234 SOL
```

If SOL balance < 0.01:
```
⚠️ Warning: Low SOL balance for gas fees!
```
→ Send more SOL to your wallet address

---

## 🎯 RUNNING LIVE TRADING

### Test Mode (RECOMMENDED FIRST)

Start with reduced position sizes and lower confidence:

```bash
python solana_auto_trader_trending.py \
    --mode live \
    --interval 30 \
    --min-confidence 0.80
```

This will:
1. Load your wallet configuration
2. Check SOL balance
3. Ask for final confirmation
4. Start trading with 15% position size

### Reduced Risk Mode

For extra safety, edit the code to reduce position size:

Open `solana_auto_trader_trending.py` and change line 86:
```python
self.max_position_pct = 0.05  # 5% instead of 15%
```

### Full Auto Mode (After Testing)

Once comfortable:

```bash
python solana_auto_trader_trending.py \
    --mode live \
    --interval 30 \
    --min-confidence 0.75
```

---

## 🎮 LIVE TRADING CONTROLS

### Start Trading

```bash
python solana_auto_trader_trending.py --mode live
```

You'll see:
```
🔴 LIVE TRADING MODE - INITIALIZING WALLET
✓ Wallet loaded: YOUR_WALLET_ADDRESS
💰 Wallet SOL Balance: 0.0234 SOL

⚠️ FINAL CONFIRMATION
Mode: LIVE TRADING
Wallet: YOUR_WALLET_ADDRESS
Position Size: 11.25% of portfolio per trade
Max Trades/Day: 240

YOU CAN LOSE ALL YOUR FUNDS!
Press Ctrl+C at any time to stop.

Type 'START' to begin live trading:
```

Type `START` and press Enter.

### Stop Trading

Press **Ctrl+C** at any time to stop. The bot will:
1. Stop taking new trades
2. Show final portfolio summary
3. Exit cleanly

**Your open positions will remain** - you'll need to close them manually or restart the bot.

### Emergency Stop

If something goes wrong:
1. Press **Ctrl+C** (may need 2-3 times)
2. Close terminal if unresponsive
3. Check Solana Explorer for your transactions

---

## 📊 MONITORING

### What to Watch

While the bot is running, monitor:

1. **Transaction Confirmations**
   ```
   ✓ Transaction sent: ABC123...
   ✓ Transaction confirmed!
   ```
   
2. **Balance Changes**
   ```
   📊 PORTFOLIO STATUS
   USDC Balance: $987.34
   Open Positions: 2
   ```

3. **Failed Transactions**
   ```
   ❌ Transaction failed
   ❌ Slippage tolerance exceeded
   ```

### Check Live Transactions

View your transactions on Solana Explorer:
```
https://solscan.io/account/YOUR_WALLET_ADDRESS
```

Or Solana FM:
```
https://solana.fm/address/YOUR_WALLET_ADDRESS
```

---

## 💰 EXPECTED COSTS

### Per Trade Costs

For a $150 trade:
- **Network fee:** ~$0.00025 (negligible)
- **Jupiter fee:** ~$0.004 (0.0025%)
- **Slippage (meme coins):** $7.50-$22.50 (5-15%)

**Total round-trip cost:** $7.50-$22.50 per buy+sell cycle

### Impact on Your Strategy

Your paper trading showed:
- Average win: +15.37%
- 6 winning trades

With real costs:
- Average win after costs: **+10%** (5% lost to slippage)
- Net profit: **~$60** instead of $122 (50% reduction)

**This is normal and expected!**

---

## 🔍 TROUBLESHOOTING

### Issue: "No wallet configuration found"

**Solution:**
```bash
python setup_wallet.py --setup
```

### Issue: "Transaction failed"

**Possible causes:**
1. Insufficient SOL for gas → Send more SOL
2. Slippage too low → Increase in config (200-500 bps)
3. Token liquidity dried up → Bot will skip it
4. Network congestion → Try again or increase priority fee

### Issue: "Low SOL balance"

**Solution:**
Send SOL to your wallet address shown in the startup message.
Minimum: 0.01 SOL (~$1-2)

### Issue: Large slippage on trades

**This is normal for meme coins!**

Strategies:
1. Increase `max_slippage_bps` in config (200-500)
2. Reduce position size (5% instead of 15%)
3. Avoid tokens with < $100k liquidity

### Issue: Trades not executing

**Check:**
1. Do you have USDC in your wallet? (Bot needs USDC to buy)
2. Is your wallet properly configured?
3. Check logs for error messages

---

## 🛡️ SECURITY BEST PRACTICES

### Do's ✅

- ✅ Use a dedicated trading wallet
- ✅ Keep private key in `.wallet_config.json` (in .gitignore)
- ✅ Start with $50-100 maximum
- ✅ Test in paper mode first (multiple sessions)
- ✅ Monitor trades actively
- ✅ Set a daily loss limit mentally
- ✅ Keep backup of your private key (offline)

### Don'ts ❌

- ❌ Use your main wallet with all your funds
- ❌ Commit `.wallet_config.json` to git
- ❌ Share your private key with anyone
- ❌ Start with large amounts ($1000+)
- ❌ Leave bot running unmonitored
- ❌ Trade tokens you don't understand
- ❌ Ignore warning messages

---

## 📈 RECOMMENDED SETTINGS

### Conservative (Start Here)

```bash
python solana_auto_trader_trending.py \
    --mode live \
    --interval 60 \
    --min-confidence 0.85 \
    --max-trades 100
```

Also edit code:
```python
self.max_position_pct = 0.05  # 5% position size
```

### Moderate (After 1 Week Success)

```bash
python solana_auto_trader_trending.py \
    --mode live \
    --interval 30 \
    --min-confidence 0.75 \
    --max-trades 200
```

Keep default:
```python
self.max_position_pct = 0.15  # 15% position size
```

### Aggressive (Only if Profitable)

```bash
python solana_auto_trader_trending.py \
    --mode live \
    --interval 30 \
    --min-confidence 0.70 \
    --max-trades 240
```

**Never increase position size beyond 15%!**

---

## 🎯 SUCCESS CRITERIA

### Week 1 Goals

- [ ] Bot runs without crashes
- [ ] Trades execute successfully
- [ ] Slippage within expectations (5-15%)
- [ ] No major losses (< 20% drawdown)
- [ ] Break even or small profit

### Week 2-4 Goals

- [ ] Consistent profitable sessions
- [ ] Average win > average loss
- [ ] Win rate > 55%
- [ ] Max drawdown < 30%
- [ ] Total return positive

### Red Flags 🚩

Stop immediately if:
- ⚠️ Down more than 50% in 24 hours
- ⚠️ Multiple failed transactions
- ⚠️ Unexpected token purchases
- ⚠️ Can't stop the bot (Ctrl+C not working)
- ⚠️ Wallet drained by gas fees

---

## 📞 GETTING HELP

### Bot Issues

1. Check terminal output for error messages
2. Review transaction on Solscan.io
3. Verify wallet balance and SOL for gas
4. Re-run setup: `python setup_wallet.py --setup`

### Lost Funds?

The bot doesn't "lose" your funds - you still own the tokens.

If down 50%:
1. Check your positions in Phantom/Solflare wallet
2. Tokens are there, just worth less
3. You can hold or sell manually
4. Or restart bot to let it manage the exit

### Transaction Stuck?

Check on Solscan.io:
- ✓ Confirmed = went through
- ⏳ Pending = wait 30-60s
- ❌ Failed = try again

---

## 🔄 SWITCHING BACK TO PAPER MODE

To test changes without risk:

```bash
python solana_auto_trader_trending.py --mode paper --balance 1000
```

Always test new settings in paper mode first!

---

## 📚 NEXT STEPS

1. **Complete Setup**
   ```bash
   python setup_wallet.py --setup
   ```

2. **Test Wallet**
   ```bash
   python wallet_integration.py --check-balance
   ```

3. **Start Conservative**
   ```bash
   python solana_auto_trader_trending.py --mode live --min-confidence 0.85
   ```

4. **Monitor Actively**
   - Keep terminal visible
   - Check Solscan regularly
   - Track P&L manually

5. **Increase Gradually**
   - Week 1: $50, 5% positions
   - Week 2: $100, 10% positions
   - Week 3: $200, 15% positions
   - Only increase if profitable!

---

## ✅ FINAL CHECKLIST

Before starting live trading:

- [ ] Installed dependencies: `pip install solana solders base58`
- [ ] Wallet configured: `.wallet_config.json` created
- [ ] SOL balance > 0.01 SOL for gas
- [ ] USDC funded in wallet ($50-100)
- [ ] Tested wallet connection successfully
- [ ] Understand you can lose everything
- [ ] Using a dedicated wallet (not main wallet)
- [ ] Know how to stop (Ctrl+C)
- [ ] Monitoring terminal actively
- [ ] Ready to start small and increase gradually

---

**You're ready to trade! 🚀**

Remember: **Start small, trade safe, and never risk more than you can afford to lose.**

Good luck! 🍀
