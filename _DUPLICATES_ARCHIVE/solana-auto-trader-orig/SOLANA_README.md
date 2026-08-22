# Solana DEX Trading AI Agent

A sophisticated cryptocurrency trading agent for Solana blockchain with Jupiter Aggregator integration. Features AI-powered analysis, multiple trading strategies, and support for both paper and live trading on Solana DEXs.

## 🌟 Key Features

### Solana-Specific Features
- **Jupiter Aggregator Integration** - Best price routing across all Solana DEXs
- **SPL Token Support** - Trade any Solana token (SOL, JUP, RAY, BONK, etc.)
- **Low Gas Fees** - Solana's sub-penny transaction costs
- **Fast Execution** - Sub-second transaction finality
- **On-Chain Trading** - True DeFi, no centralized exchange needed

### Core Functionality
- **Dual Trading Modes**: Paper trading and live on-chain trading
- **AI-Powered Analysis**: Technical indicators (RSI, MACD, SMA, EMA)
- **Multiple Strategies**: Momentum, mean reversion, breakout detection
- **Autonomous Trading**: 24/7 automated execution with risk management
- **Portfolio Tracking**: Real-time P&L and performance metrics
- **Safety Features**: Stop loss, take profit, position limits

## 📋 Supported Tokens

| Token | Symbol | Description |
|-------|--------|-------------|
| Solana | SOL | Native Solana token |
| USD Coin | USDC | Stablecoin (base currency) |
| Tether | USDT | Stablecoin |
| Raydium | RAY | DEX token |
| Jupiter | JUP | Aggregator token |
| Bonk | BONK | Meme coin |
| dogwifhat | WIF | Meme coin |
| Jito | JTO | MEV token |
| Pyth | PYTH | Oracle token |

*More tokens can be easily added by updating the TOKENS dictionary*

## 🚀 Installation

### Prerequisites
- Python 3.8 or higher
- Solana wallet (for live trading)
- Basic understanding of DeFi and Solana

### Quick Setup

```bash
# Install dependencies
pip install requests

# For live trading (optional)
pip install solana solders
```

### File Structure
```
solana-trading-ai/
├── solana_trading_agent.py      # Main trading agent
├── solana_auto_trader.py        # Autonomous trading
├── SOLANA_README.md             # This file
└── SOLANA_QUICKSTART.md         # Beginner guide
```

## 🎮 Usage

### Manual Trading (Paper Mode)

```bash
# Start the agent
python solana_trading_agent.py

# Available commands
solana-ai> add SOL          # Add SOL to watchlist
solana-ai> add JUP          # Add Jupiter
solana-ai> update           # Fetch prices
solana-ai> analyze SOL      # Technical analysis
solana-ai> buy SOL 100      # Buy $100 worth of SOL
solana-ai> sell SOL 0.5     # Sell 0.5 SOL
solana-ai> portfolio        # View holdings
```

### Autonomous Trading

```bash
# Paper trading (recommended first)
python solana_auto_trader.py --tokens SOL JUP RAY

# Live trading
python solana_auto_trader.py \
  --mode live \
  --wallet YOUR_WALLET_ADDRESS \
  --tokens SOL JUP
```

## 💡 Example Session

```bash
$ python solana_trading_agent.py

============================================================
SOLANA DEX TRADING AI AGENT
============================================================
Mode: PAPER
DEX: Jupiter Aggregator
Initial USDC: $1,000.00
============================================================

Available Tokens:
SOL, USDC, USDT, RAY, BONK, JUP, WIF, JTO, PYTH

solana-ai> add SOL
✓ Added SOL to watch list

solana-ai> add JUP
✓ Added JUP to watch list

solana-ai> update
Updating market data...
✓ Market data updated

solana-ai> analyze SOL

============================================================
TECHNICAL ANALYSIS: SOL
============================================================

Current Price: $110.234567
Signal: BUY (Confidence: 72.0%)
Trend: BULLISH - STRONG

Indicators:
  RSI: 34.52
  SMA(20): $108.456789
  SMA(50): $106.123456
  MACD: 0.123456

Reason: RSI indicates oversold condition; Price above moving averages
============================================================

solana-ai> buy SOL 100

✓ Swap executed: USDC → SOL
  Amount In: 100.000000 USDC
  Amount Out: 0.906977 SOL
  Price: $110.262341
  TX: sim_1707842732_1234

solana-ai> portfolio

============================================================
SOLANA PORTFOLIO STATUS
============================================================

USDC Balance: $900.00
Total Value: $1,000.23
Total Return: $0.23 (0.02%)
Total Swaps: 1

------------------------------------------------------------
OPEN POSITIONS
------------------------------------------------------------
Token      Amount          Avg Price    Current      P&L            
------------------------------------------------------------
SOL        0.906977        $110.2623    $110.2623    +$0.00 (+0.00%)
============================================================
```

## 🤖 Autonomous Trading

The autonomous mode continuously monitors markets and trades automatically:

```bash
python solana_auto_trader.py --tokens SOL JUP RAY
```

**Features:**
- Updates prices every 30 seconds
- Analyzes all watched tokens
- Executes trades when signals are strong
- Automatic stop loss (8%) and take profit (15%)
- Trade cooldown (5 min per token)
- Daily trade limit (15 swaps/day)

**Output:**
```
============================================================
  🤖 SOLANA AUTONOMOUS TRADING ACTIVATED
============================================================

Configuration:
  Mode: PAPER
  DEX: Jupiter Aggregator
  Update Interval: 30s
  Min Confidence: 70%
  Watched Tokens: SOL, JUP, RAY

============================================================
Cycle #1 - 2024-02-13 17:30:00
============================================================

📊 Updating market data...

🔍 Analyzing tokens...

[17:30:05] SOL
  Price: $110.456789
  Signal: BUY (Confidence: 75.0%)
  RSI: 32.1 | Trend: BULLISH

  🤖 AUTO-BUY TRIGGERED
     Token: SOL
     USDC Amount: $150.00
     Expected Price: $110.456789
     Confidence: 75.0%

  ✓ Swap executed: USDC → SOL
  ✓ Trade executed successfully!
```

## 🛡️ Safety Features

### Automatic Risk Management
- **Stop Loss**: Auto-sells at 8% loss (configurable)
- **Take Profit**: Auto-sells at 15% gain (configurable)
- **Position Limits**: Max 15% of portfolio per token
- **Trade Cooldown**: 5 minutes between trades per token
- **Daily Limits**: Maximum 15 swaps per day
- **Confidence Filter**: Only trades signals >70%

### Paper Trading Benefits
- Zero financial risk
- Test strategies safely
- Learn market dynamics
- Perfect for beginners
- Unlimited practice

## 🔧 Configuration

### Command-Line Options

**Manual Trading:**
```bash
--mode {paper|live}     # Trading mode (default: paper)
--wallet ADDRESS        # Solana wallet address (live mode)
--balance AMOUNT        # Initial USDC for paper (default: 1000)
```

**Autonomous Trading:**
```bash
--mode {paper|live}     # Trading mode
--wallet ADDRESS        # Wallet address
--balance AMOUNT        # Initial USDC
--interval SECONDS      # Update interval (default: 30)
--min-confidence PCT    # Min confidence (default: 0.70)
--tokens SYM1 SYM2     # Tokens to trade
```

### Examples

```bash
# Conservative paper trading
python solana_auto_trader.py \
  --min-confidence 0.80 \
  --interval 60 \
  --tokens SOL

# Aggressive multi-token
python solana_auto_trader.py \
  --min-confidence 0.65 \
  --interval 15 \
  --tokens SOL JUP RAY BONK

# Live trading (small start)
python solana_auto_trader.py \
  --mode live \
  --wallet YOUR_WALLET \
  --balance 100 \
  --tokens SOL
```

## 📊 Technical Indicators

### RSI (Relative Strength Index)
- **< 30**: Oversold (potential buy)
- **> 70**: Overbought (potential sell)
- **40-60**: Neutral zone

### Moving Averages
- **SMA(20)**: Short-term trend
- **SMA(50)**: Medium-term trend
- **Bullish**: Price > SMA
- **Bearish**: Price < SMA

### MACD
- **Bullish**: MACD > Signal line
- **Bearish**: MACD < Signal line
- **Strength**: Distance between lines

## 🌐 Jupiter Aggregator

Jupiter finds the best swap routes across all Solana DEXs:
- Raydium
- Orca
- Serum
- Saber
- And 10+ more

**Benefits:**
- Best prices
- Lower slippage
- Smart routing
- Aggregated liquidity

## 🔐 Live Trading Setup

### Requirements
1. **Solana Wallet**
   - Phantom, Solflare, or any SPL wallet
   - Fund with SOL for gas fees
   - Fund with USDC for trading

2. **Python Packages**
   ```bash
   pip install solana solders
   ```

3. **Wallet Integration**
   - Export private key (securely!)
   - Or use hardware wallet
   - Never share your seed phrase

### Security Best Practices
- Start with small amounts ($50-100)
- Test in paper mode extensively first
- Use a dedicated trading wallet
- Keep majority of funds in cold storage
- Never share private keys
- Monitor trades actively

## 📈 Performance Tips

### For Best Results
1. **Start with paper trading** (minimum 1 week)
2. **Test multiple tokens** to diversify
3. **Monitor during different market conditions**
4. **Adjust confidence threshold** based on performance
5. **Keep detailed notes** of all trades

### Recommended Settings

**Conservative:**
- Confidence: 0.80
- Interval: 60s
- Tokens: 1-2 (SOL, USDC)
- Max position: 10%

**Balanced:**
- Confidence: 0.70
- Interval: 30s
- Tokens: 2-3 (SOL, JUP, RAY)
- Max position: 15%

**Aggressive:**
- Confidence: 0.65
- Interval: 15s
- Tokens: 3-5
- Max position: 20%

## 🚨 Important Disclaimers

⚠️ **Critical Information:**
- Cryptocurrency trading is highly risky
- You can lose all your investment
- Past performance ≠ future results
- This is NOT financial advice
- Always do your own research (DYOR)
- Start with amounts you can afford to lose

⚠️ **Solana-Specific Risks:**
- Network outages can occur
- Transaction failures possible
- Smart contract risks
- Impermanent loss on DEXs
- Rug pulls in meme coins

## 🆚 Comparison: Solana vs CoinDCX Agent

| Feature | Solana Agent | CoinDCX Agent |
|---------|--------------|---------------|
| **Exchange Type** | DEX (Decentralized) | CEX (Centralized) |
| **Trading** | On-chain swaps | Order book |
| **Custody** | Your wallet | Exchange wallet |
| **Gas Fees** | $0.00025 (SOL) | Trading fees |
| **Speed** | <1 second | Varies |
| **KYC Required** | No | Yes |
| **Tokens** | 1000s of SPL tokens | Limited pairs |
| **Liquidity** | Aggregated DEX | CoinDCX only |

## 🛠️ Advanced Features

### Adding New Tokens

Edit `solana_trading_agent.py`:
```python
TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "MYNEWTOKEN": "YOUR_TOKEN_MINT_ADDRESS_HERE",
    # Add more tokens...
}
```

### Custom Strategies

The agent uses the same strategy system as the CoinDCX version:
- Momentum strategy
- Mean reversion strategy
- Breakout strategy
- Consensus mode (recommended)

### Logging

All trades are logged with:
- Timestamp
- Token pair
- Amounts
- Prices
- Transaction signatures
- Strategy used

## 📚 Additional Resources

### Solana Ecosystem
- [Solana Documentation](https://docs.solana.com)
- [Jupiter Aggregator](https://jup.ag)
- [Solana Explorer](https://explorer.solana.com)

### Trading Education
- Technical analysis basics
- DeFi fundamentals
- Risk management principles
- Solana token economics

## 🐛 Troubleshooting

### Common Issues

**"Unable to get quote"**
- Check internet connection
- Verify token addresses
- Try again in a moment

**"Insufficient USDC"**
- Check portfolio balance
- Reduce trade amount
- Add more USDC

**"Transaction failed"**
- Network congestion
- Insufficient SOL for gas
- Retry transaction

### Getting Help

1. Read this documentation
2. Check SOLANA_QUICKSTART.md
3. Test in paper mode first
4. Start with small amounts

## 📝 Quick Reference

### Common Commands
```bash
add SOL              # Watch SOL
update               # Fetch prices
analyze SOL          # Technical analysis
buy SOL 100          # Buy $100 of SOL
sell SOL 0.5         # Sell 0.5 SOL
portfolio            # View holdings
list                 # Show watchlist
help                 # Show commands
quit                 # Exit
```

### Token Shortcuts
- **SOL** - Solana
- **JUP** - Jupiter
- **RAY** - Raydium
- **BONK** - Bonk
- **WIF** - dogwifhat
- **JTO** - Jito
- **PYTH** - Pyth Network

## 🎯 Next Steps

1. ✅ Install dependencies
2. ✅ Run in paper mode
3. ✅ Add tokens to watchlist
4. ✅ Practice trading
5. ✅ Test autonomous mode
6. ⚠️ Consider live trading (carefully!)

---

**Ready to trade on Solana?**

```bash
# Start paper trading
python solana_trading_agent.py

# Add your favorite tokens
solana-ai> add SOL
solana-ai> add JUP

# Start trading!
```

🚀 **Happy Solana Trading!**

---

*Built with Jupiter Aggregator | Powered by Solana blockchain*
