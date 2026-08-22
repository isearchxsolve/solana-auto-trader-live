# 🚀 Solana Hybrid Sniper Ultra

A production-grade Solana meme token trading bot combining three dynamic strategies:

| Mode | Trigger | Goal |
|------|---------|------|
| 🎯 **SNIPER** | token age < 120s, micro-cap, vol spike 300%+ | Ultra-fast new-token launch entry |
| 🚀 **MG** (Mega Growth) | medium vol, strong momentum, pump_prob > 60% | Trend pyramiding with up to 3 entries |
| 🛡️ **HWR** (High Win Rate) | low vol, high liquidity, pump_prob > 80% | Conservative, precision entries only |

The system **auto-switches modes** using XGBoost ML predictions on every iteration.

---

## 📁 Project Structure

```
solana_hybrid_sniper_ultra/
  ├── bot.py              # Main async engine
  ├── strategy.py         # Mode selection + entry/exit decisions
  ├── features.py         # Feature engineering (standard + sniper)
  ├── executor_paper.py   # Paper trading simulator
  ├── server.py           # FastAPI monitoring dashboard
  ├── utils/
  │     ├── data_fetcher.py    # DexScreener + Birdeye API clients
  │     ├── indicators.py      # EMA, RSI, ATR, Bollinger
  │     ├── helpers.py         # Retry decorators, unit conversions
  │     ├── solana_client.py   # Solana RPC wrapper (solders)
  │     ├── helius.py          # Helius enhanced RPC
  │     └── swap_executor.py   # Jupiter v6 swap pipeline
  ├── ml/
  │     ├── train.py           # XGBoost training
  │     ├── predict.py         # Inference wrapper
  │     └── model.pkl          # Saved model (generated after training)
  ├── data/
  │     └── paper_trades.csv   # Auto-generated trade log
  ├── logs/
  │     └── bot.log
  ├── requirements.txt
  └── .env.example
```

---

## ⚡ Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env with your API keys
```

### 3. Train the ML model
```bash
python -m ml.train
# Uses synthetic data by default.
# For real labels: python -m ml.train --data data/labeled.csv
```

### 4. Run the bot (paper mode)
```bash
python bot.py
```

### 5. Launch dashboard
```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```
Visit `http://localhost:8000/signals` for live ML signals.

---

## 🔴 Live Trading

Set in `.env`:
```
MODE="live"
PRIVATE_KEY="your_base58_private_key"
HELIUS_API_KEY="your_helius_key"
BIRDEYE_API_KEY="your_birdeye_key"
```

> ⚠️ **WARNING**: Live trading with real funds carries significant risk. Always test in paper mode first. Never trade more than you can afford to lose.

---

## 🧠 Strategy Logic

### SNIPER MODE (Priority 1)
Triggers when token is < 120 seconds old with liquidity < $150k and volume spiking 300%+.
Enters immediately on first valid candle pattern. Exits on any liquidity drop or dump signal.

### MG MODE (Priority 2)
Enters on strong momentum with volume confirmation. Supports up to 3 pyramided entries
as trend_strength increases. Exits when dump_risk exceeds 55% or price falls below EMA5.

### HWR MODE (Priority 3)
High-precision entry requiring pump_probability > 80% and dump_risk < 30%.
EMA10 > EMA20 + RSI 55-70 required. Lowest frequency, highest confidence.

---

## 📊 API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Health check |
| `GET /status` | Bot summary (PnL, trades, positions) |
| `GET /mode` | Current active mode + history |
| `GET /positions` | Open positions |
| `GET /predictions` | ML predictions for all tracked tokens |
| `GET /signals` | Raw signals with mode decisions |
| `GET /trades` | Paper trade history |
| `GET /pnl` | PnL breakdown by mode |

---

## 🔑 Environment Variables

| Variable | Description |
|----------|-------------|
| `MODE` | `"paper"` or `"live"` |
| `RPC_URL` | Helius or other Solana RPC URL |
| `PRIVATE_KEY` | Base58 wallet private key (live only) |
| `JUPITER_BASE_URL` | Jupiter v6 API base |
| `BIRDEYE_API_KEY` | Birdeye API key |
| `HELIUS_API_KEY` | Helius API key |
| `BASE_TOKEN` | SOL mint address (default: wrapped SOL) |
| `TRADE_AMOUNT_SOL` | Per-trade size in SOL |
| `MAX_OPEN_POSITIONS` | Maximum concurrent positions |

---

## 📦 Dependencies

- `aiohttp` — Async HTTP
- `xgboost` / `scikit-learn` — ML
- `solders` — Solana transaction building/signing
- `fastapi` / `uvicorn` — Dashboard
- `pandas` / `numpy` — Data processing
- `python-dotenv` — Config management

---

## ⚖️ Disclaimer

This software is for **educational and research purposes only**. Cryptocurrency trading involves
substantial risk of loss. Past performance does not guarantee future results.
The authors are not responsible for any financial losses incurred through use of this software.
