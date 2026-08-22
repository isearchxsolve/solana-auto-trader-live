# config.py
# PRODUCTION CONFIGURATION — ALL SETTINGS IN ONE PLACE

# ============================================================
# TRADING MODE
# "PAPER" = simulated trades only
# "REAL"  = live execution (requires executor)
# ============================================================
MODE = "PAPER"

# ============================================================
# PAPER TRADING
# ============================================================
PAPER_STARTING_CASH = 1000.0

# ============================================================
# TOKEN UNIVERSE
# Solana pair addresses from DexScreener.
# Format: { "symbol": str, "address": str (pair address) }
# ============================================================
TOKEN_UNIVERSE = [
    {"symbol": "SOL/USDC",  "address": "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj"},
    {"symbol": "BONK/SOL",  "address": "Hs97TCZeuYiJxooo3U73qEHXg3dKpRL4uYKYRryEK9CF"},
    {"symbol": "WIF/SOL",   "address": "EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2oundjskjkd6Hp7"},
    {"symbol": "JUP/USDC",  "address": "C1MgLojNLWBKADvu9BHdtgzz1oZX4dZ5zGdGcgvvW5Ku"},
    {"symbol": "PYTH/USDC", "address": "CapuXNQoDviLvU1PxFiizLgPNQCxrsag1uMeyk6zLVps"},
]

# ============================================================
# CANDLE SETTINGS
# ============================================================
CANDLE_TIMEFRAME = "15m"         # "1m", "5m", "15m"
CANDLE_LIMIT     = 500           # candles to fetch per token
MIN_CANDLES      = 60            # minimum candles required for TA/ML

# ============================================================
# LOOP TIMING
# ============================================================
LOOP_DELAY_SECONDS    = 15       # seconds between full universe scans
UNIVERSE_ROTATE_EVERY = 10       # rotate universe every N loops
API_TIMEOUT           = 10       # seconds per API call
API_RETRIES           = 3        # retries on failure

# ============================================================
# RISK MANAGEMENT
# ============================================================
MAX_POSITIONS          = 5       # maximum simultaneous open positions
BASE_POSITION_FRACTION = 0.20    # fraction of cash per trade
MAX_POSITION_FRACTION  = 0.35    # absolute cap
MIN_POSITION_FRACTION  = 0.05

STOP_LOSS_PCT   = 0.05           # 5% stop loss
TAKE_PROFIT_PCT = 0.12           # 12% take profit

MIN_LIQUIDITY_USD = 10_000       # skip tokens below this liquidity

# ============================================================
# ML THRESHOLDS
# ============================================================
UP_THRESHOLD       = 0.65
DOWN_THRESHOLD     = 0.55
BREAKOUT_THRESHOLD = 0.55

# ============================================================
# CHAIN
# ============================================================
CHAIN = "solana"

# ============================================================
# DIRECTORIES
# ============================================================
MODEL_DIR = "models"
CACHE_DIR = "data_cache"
LOG_DIR   = "logs"
