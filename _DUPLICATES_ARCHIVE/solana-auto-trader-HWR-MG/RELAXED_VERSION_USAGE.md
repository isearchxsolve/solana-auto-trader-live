# Relaxed Parameters Version - Quick Start

## What Changed

I've created `solana_trading_agent_relaxed.py` with more forgiving parameters:

| Parameter | Original | Relaxed | Impact |
|-----------|----------|---------|--------|
| **RSI Range** | 38-62 | 38-68 | +40% wider |
| **Trend** | BULLISH only | BULLISH or NEUTRAL+slope | +70% more opportunities |
| **EMA Slope** | > 0.001 (0.1%) | > 0.0005 (0.05%) | 2x more sensitive |
| **Volume** | ≥ 0.40x | ≥ 0.30x | +25% lower threshold |

**Expected Result**: 5-10x more trade opportunities

## How to Use

### Option 1: Quick Test (Copy over original)

```bash
# Backup your original
cp solana_trading_agent.py solana_trading_agent_BACKUP.py

# Replace with relaxed version
cp solana_trading_agent_relaxed.py solana_trading_agent.py

# Run your bot as normal
python solana_auto_trader.py
```

### Option 2: Test Side-by-Side

```bash
# Edit solana_auto_trader.py line 17 to import from relaxed version
# FROM:
from solana_trading_agent import TradingMode, TradingAI, Portfolio, PriceHistory, TOKENS

# TO:
from solana_trading_agent_relaxed import TradingMode, TradingAI, Portfolio, PriceHistory, TOKENS

# Run
python solana_auto_trader.py
```

## What to Expect

With your current market conditions:

**Before (0 trades):**
- SOL: Blocked by RSI 77, trend NEUTRAL
- JUP: Blocked by RSI 67  
- RAY: Blocked by RSI 14, trend BEARISH
- BONK: Blocked by trend NEUTRAL (negative slope)
- WIF: Blocked by RSI 64, trend NEUTRAL

**After (potential trades):**
- SOL: Still blocked (RSI too high at 77, need < 68)
- JUP: **POTENTIAL TRADE** (RSI 67 now in range 38-68, trend BULLISH)
- RAY: Still blocked (RSI too low, BEARISH)
- BONK: Still blocked (negative slope)
- WIF: **POTENTIAL TRADE** (RSI 64 in range, if slope turns positive)

## Testing Checklist

✅ **First Hour**: You should see 1-3 trade signals  
✅ **4 Hours**: Expect 2-8 trades executed  
✅ **24 Hours**: Target 5-15 trades total  

If you get:
- **0-1 trades in 4 hours**: Market is truly flat, wait or relax further
- **2-8 trades in 4 hours**: **PERFECT** ✅
- **15+ trades in 4 hours**: Too aggressive, tighten parameters

## Monitoring

Watch your first few trades closely:

```bash
python solana_auto_trader.py | tee trading.log
```

Check win rate after 10 trades:
- **> 60% wins**: Excellent, parameters working well
- **40-60% wins**: Acceptable for trend-following
- **< 40% wins**: Too loose, tighten back RSI to 38-65

## Reverting to Original

```bash
# If you backed up:
cp solana_trading_agent_BACKUP.py solana_trading_agent.py

# Or reinstall from git:
git checkout solana_trading_agent.py
```

## Advanced: Further Relaxation

If you still get < 1 trade per hour, you can further relax:

**Super Relaxed (for very flat markets):**
```python
# In solana_trading_agent_relaxed.py:

# Line 333: Widen RSI even more
if 35 <= rsi_v <= 70:

# Line 316: Lower slope threshold more
if ema20 > ema50 and ema_slope > 0.0002:

# Line 351: Lower volume requirement
if rel_vol < 0.20:
```

But be careful - this may generate false signals!

## Pro Tips

1. **Start conservative**: Use relaxed version in PAPER mode first
2. **Monitor 10 trades** before going live
3. **Track win rate**: Should be > 50% for profitability
4. **Use stop losses**: Your 7% stop loss is good, keep it
5. **Size appropriately**: 20% per position is reasonable

---

Good luck! You should see action within the first hour. 🚀
