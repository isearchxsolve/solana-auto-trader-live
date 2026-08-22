# Auto-Trending Token Trading Guide

## 🔥 Automatic Trending Token Discovery

The enhanced Solana trader **automatically discovers and trades trending tokens** without manual selection!

## How It Works

```
1. Scans DexScreener API every hour
2. Finds trending Solana tokens
3. Ranks by volume, liquidity, price change
4. Filters out risky/scam tokens
5. Auto-selects top 5-10 tokens
6. Updates watchlist automatically
7. Trades the best signals
```

## Quick Start

### Fully Automatic (Recommended)

```bash
# Just start it - no token selection needed!
python solana_auto_trader_trending.py
```

That's it! The agent will:
- ✅ Find trending tokens automatically
- ✅ Refresh every hour
- ✅ Trade the hottest tokens
- ✅ Avoid scams and low liquidity
- ✅ Manage everything for you

### Example Output

```
============================================================
  🤖 SOLANA AUTO-TRENDING TRADER ACTIVATED
============================================================

Configuration:
  Mode: PAPER
  Auto-Discovery: ✅ ENABLED
  Update Interval: 30s
  Trending Refresh: Every 1 hour

============================================================

🔍 Discovering trending tokens...

============================================================
🔥 TRENDING SOLANA TOKENS
============================================================
Rank   Symbol       Price           24h Vol         24h Change  Score   
--------------------------------------------------------------------------------
1      JUP          $0.82541200     $45,234,567     🟢 +12.34%  87.5    
2      RAY          $1.52341000     $32,456,789     🟢 +8.92%   82.3    
3      BONK         $0.00002134     $28,765,432     🟢 +15.67%  79.1    
4      WIF          $2.34567000     $25,432,100     🟢 +6.45%   76.8    
5      JTO          $3.12345000     $21,234,567     🟢 +4.23%   72.4    
================================================================================

✅ AUTO-SELECTED TOKENS FOR TRADING:
------------------------------------------------------------
  1. JUP          (Score: 87.5) 🆕 NEW
  2. RAY          (Score: 82.3) 🆕 NEW
  3. BONK         (Score: 79.1) 🆕 NEW
  4. WIF          (Score: 76.8) 🆕 NEW
  5. JTO          (Score: 72.4) 🆕 NEW
------------------------------------------------------------

Press Ctrl+C to stop

============================================================
```

## Configuration Options

### Basic Usage

```bash
# Default settings (recommended)
python solana_auto_trader_trending.py

# Paper trading with $5000
python solana_auto_trader_trending.py --balance 5000

# Higher confidence for safety
python solana_auto_trader_trending.py --min-confidence 0.80

# Faster updates
python solana_auto_trader_trending.py --interval 15
```

### Advanced Options

```bash
# Refresh trending list every 30 minutes
python solana_auto_trader_trending.py --refresh-interval 1800

# Live trading (requires wallet)
python solana_auto_trader_trending.py \
  --mode live \
  --wallet YOUR_WALLET_ADDRESS

# Disable auto-discovery (manual mode)
python solana_auto_trader_trending.py \
  --no-auto-discover \
  --manual-tokens SOL JUP RAY
```

## Trending Token Selection Criteria

### ✅ Tokens ARE Selected If:

1. **High Liquidity**: > $100,000 USD
2. **High Volume**: > $200,000 24h volume
3. **Positive Momentum**: Rising price trends
4. **Safety Checks**: Pass scam filters
5. **High Score**: Top ranked by algorithm

### ❌ Tokens Are NOT Selected If:

1. **Low Liquidity**: < $100,000 USD
2. **Low Volume**: < $200,000 24h
3. **Extreme Pump**: > 1000% in 24h (suspicious)
4. **Stablecoins**: USDC, USDT, etc.
5. **Failed Safety**: Fail risk filters

## Scoring Algorithm

```
Trend Score = Volume Score + Liquidity Score + Price Change Score

Volume Score:      0-30 points (based on 24h volume)
Liquidity Score:   0-30 points (based on total liquidity)
Price Change:      -20 to +40 points (based on 24h change)

Total:             Max 100 points
```

### Score Meanings

- **80-100**: 🔥 Very hot, high confidence
- **70-80**: ⭐ Strong trending
- **60-70**: ✅ Moderate trending
- **Below 60**: Not selected

## Safety Features

### Automatic Filters

1. **Liquidity Filter**
   - Minimum $100k liquidity
   - Prevents low liquidity scams

2. **Volume Filter**
   - Minimum $200k daily volume
   - Ensures tradeable markets

3. **Volatility Filter**
   - Blocks >1000% pumps
   - Avoids obvious scams

4. **Age Check** (planned)
   - Prefer established tokens
   - Avoid brand new tokens

### Risk Management

- **Stop Loss**: Auto-sell at 8% loss
- **Take Profit**: Auto-sell at 15% gain
- **Position Limits**: Max 15% per token
- **Diversification**: Trades 5-10 tokens
- **Cooldown**: 5 min between trades

## Comparison: Auto vs Manual

| Feature | Auto-Trending | Manual Selection |
|---------|---------------|------------------|
| **Token Selection** | Automatic | You choose |
| **Updates** | Every hour | When you want |
| **Research Needed** | None | Required |
| **Adaptability** | High | Low |
| **New Tokens** | Auto-discovers | Must add manually |
| **Time Required** | Set & forget | Ongoing monitoring |
| **Best For** | Busy traders | Hands-on traders |

## When to Use Auto-Trending

### ✅ Use Auto-Trending If:

- You want hands-off trading
- You don't know which tokens to pick
- You want to catch new trends early
- You prefer algorithm selection
- You trade frequently
- You want diversification

### ❌ Use Manual Selection If:

- You have specific tokens in mind
- You deeply research each token
- You prefer control over selection
- You trade few tokens
- You have strong opinions

## Monitoring & Control

### What Gets Tracked

```
📊 Every Cycle:
- Trending token rankings
- Price changes
- Trading signals
- Portfolio performance

🔄 Every Hour:
- Refresh trending list
- Add new hot tokens
- Remove declining tokens
```

### Manual Override

You can always:
- Stop the agent (Ctrl+C)
- Review trending list
- Manually trade specific tokens
- Restart with manual selection

## Performance Tips

### For Best Results

1. **Run for 24+ hours**
   - See full market cycle
   - Multiple trending refreshes
   - Better performance data

2. **Start Conservative**
   - High confidence (0.75-0.80)
   - Smaller positions (10%)
   - Watch first day closely

3. **Monitor Trends**
   - Note which tokens perform well
   - Adjust confidence if needed
   - Learn from the selections

4. **Review Performance**
   - Check win rate daily
   - Identify best tokens
   - Optimize settings

### Settings by Risk Level

**Conservative:**
```bash
--min-confidence 0.80
--refresh-interval 7200  # 2 hours
```

**Balanced (Default):**
```bash
--min-confidence 0.70
--refresh-interval 3600  # 1 hour
```

**Aggressive:**
```bash
--min-confidence 0.65
--refresh-interval 1800  # 30 minutes
```

## Troubleshooting

### "No trending tokens found"

**Causes:**
- API temporarily down
- Network issues
- Very strict filters

**Solutions:**
- Wait and retry
- Lower min_liquidity
- Check internet connection

### "Too many tokens selected"

**Not a problem!** The agent:
- Analyzes all tokens
- Only trades best signals
- Manages positions automatically

### "Tokens changing too frequently"

**This is normal:**
- Market is dynamic
- Trending changes hourly
- Old positions kept until profitable

**To reduce churn:**
```bash
--refresh-interval 7200  # Refresh less often
```

## Advanced Features

### Custom Filters

Edit `trending_tokens.py`:

```python
# In AutoTokenSelector class
self.preferences = {
    'min_liquidity': 200000,    # Increase for safety
    'min_volume': 500000,       # Higher volume only
    'max_tokens': 3,            # Fewer tokens
    'prefer_rising': True,      # Only rising tokens
}
```

### Multiple Strategies

Combine with manual tokens:

```bash
# Auto-discover + always keep SOL
python solana_auto_trader_trending.py \
  --manual-tokens SOL

# Then it adds trending tokens automatically
```

## Real-World Example

### Day 1: Morning

```
🔍 Discovering trending tokens...

Selected: JUP, RAY, BONK, WIF, JTO

Cycle #1:
- Bought JUP @ $0.82 (BUY signal 75%)
- Bought RAY @ $1.52 (BUY signal 72%)
```

### Day 1: Afternoon

```
Cycle #50:
- JUP up 5% - holding
- RAY up 3% - holding
- Analyzing BONK (neutral signal)
```

### Day 1: Evening

```
🎯 TAKE PROFIT: JUP (+15.2%)
✓ Sold JUP for $950.12

Portfolio: $1,015.23 (+1.52%)
```

### Day 2: Morning

```
🔍 Refreshing trending tokens...

New trending: PYTH, ORCA
Removed: JTO (low volume)

Updated watchlist:
RAY, BONK, WIF, PYTH, ORCA
```

## FAQs

**Q: How often do tokens change?**  
A: Trending list refreshes every hour by default. Not all tokens change each time.

**Q: Will it sell my positions when tokens aren't trending?**  
A: No! It keeps positions until stop loss, take profit, or sell signal.

**Q: Can I manually add tokens too?**  
A: Yes! Use `--manual-tokens` to always keep certain tokens.

**Q: Is auto-trending riskier?**  
A: No - same risk management applies. May discover opportunities sooner.

**Q: Does it cost more (API calls)?**  
A: No - DexScreener API is free.

**Q: Can I see trending without trading?**  
A: Yes! Run `python trending_tokens.py` to just see trends.

## Comparison: Regular vs Trending

### Regular Auto Trader
```bash
python solana_auto_trader.py --tokens SOL JUP RAY
```
- ✅ You control which tokens
- ✅ Focused strategy
- ❌ Miss new trends
- ❌ Manual updates needed

### Trending Auto Trader
```bash
python solana_auto_trader_trending.py
```
- ✅ Auto-discovers trends
- ✅ Catches new pumps
- ✅ No research needed
- ✅ Adapts to market
- ⚠️ More dynamic

## Success Checklist

- [ ] Understand trending selection
- [ ] Run in paper mode first
- [ ] Monitor for 24 hours
- [ ] Review trending refreshes
- [ ] Check which tokens succeed
- [ ] Adjust confidence if needed
- [ ] Consider live trading

---

## Quick Command Reference

```bash
# Basic start
python solana_auto_trader_trending.py

# See trending tokens only
python trending_tokens.py

# Conservative settings
python solana_auto_trader_trending.py \
  --min-confidence 0.80 \
  --refresh-interval 7200

# Aggressive settings
python solana_auto_trader_trending.py \
  --min-confidence 0.65 \
  --refresh-interval 1800 \
  --interval 15

# Manual mode (no auto-discovery)
python solana_auto_trader_trending.py \
  --no-auto-discover \
  --manual-tokens SOL JUP RAY
```

---

🔥 **The future of crypto trading is automatic!**

Let the AI discover and trade trending tokens for you 24/7.
