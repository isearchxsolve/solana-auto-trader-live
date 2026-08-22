#!/usr/bin/env python3
"""
Real-time Market Conditions Monitor
Shows exactly which entry conditions are met/not met for each token

Usage:
  python market_monitor.py                    # Monitor all tokens
  python market_monitor.py --tokens SOL JUP   # Specific tokens only
  python market_monitor.py --relaxed          # Use relaxed parameters
"""

import sys
import time
import argparse
from datetime import datetime
from typing import Dict, List

# Import from your trading agent
try:
    from solana_trading_agent import TradingAI, PriceHistory, TOKENS
except ImportError:
    print("❌ Error: solana_trading_agent.py not found")
    print("   Make sure you're running this from the same directory")
    sys.exit(1)


class MarketMonitor:
    """Real-time monitor of trading conditions"""
    
    def __init__(self, tokens: List[str], relaxed: bool = False):
        self.tokens = tokens
        self.ai = TradingAI()
        self.prices = PriceHistory()
        self.relaxed = relaxed
        
        # Candle history
        self.closes = {t: [] for t in tokens}
        self.volumes = {t: [] for t in tokens}
        
        # Parameter thresholds (adjust if using relaxed version)
        self.rsi_min = 38
        self.rsi_max = 68 if relaxed else 62
        self.slope_threshold = 0.0005 if relaxed else 0.001
        self.vol_threshold = 0.30 if relaxed else 0.40
    
    def initialize(self):
        """Load initial data"""
        print("🔍 Discovering pairs and loading history...")
        self.prices.discover_all(self.tokens)
        
        for sym in self.tokens:
            if sym not in self.prices.pair_map:
                print(f"  ⚠️  {sym}: No pool found")
                continue
            
            candles = self.prices.fetch_candles(sym, timeframe="minute", 
                                                aggregate=5, limit=200)
            if candles:
                self.closes[sym] = [c["close"] for c in candles]
                self.volumes[sym] = [c["vol"] for c in candles]
                print(f"  ✓ {sym}: {len(candles)} candles loaded")
    
    def update_prices(self):
        """Fetch latest prices"""
        for sym in self.tokens:
            price = self.prices.get_live_price(sym)
            if price > 0:
                self.closes[sym].append(price)
                self.volumes[sym].append(1_000_000)
                
                if len(self.closes[sym]) > 200:
                    self.closes[sym].pop(0)
                    self.volumes[sym].pop(0)
    
    def analyze_detailed(self, sym: str) -> Dict:
        """Detailed analysis showing each condition"""
        closes = self.closes[sym]
        volumes = self.volumes[sym]
        
        if len(closes) < 60:
            return {"ready": False, "reason": f"Warming up ({len(closes)}/60)"}
        
        # Run standard analysis
        analysis = self.ai.analyze(sym, closes, volumes)
        
        # Extract components
        cur = closes[-1]
        ema20 = self.ai.ema(closes, 20)
        ema50 = self.ai.ema(closes, 50)
        rsi = analysis.get('rsi', 50)
        trend = analysis.get('trend', 'UNKNOWN')
        ema_slope = analysis.get('ema_slope', 0)
        
        # Check each condition
        conditions = {}
        
        # 1. Trend
        if self.relaxed:
            trend_ok = trend == "BULLISH" or (trend == "NEUTRAL" and ema_slope > 0)
        else:
            trend_ok = trend == "BULLISH"
        conditions['trend'] = {
            'met': trend_ok,
            'value': trend,
            'detail': f"Slope: {ema_slope:+.4f}",
            'required': "BULLISH" + (" or NEUTRAL+slope" if self.relaxed else "")
        }
        
        # 2. RSI range
        rsi_ok = self.rsi_min <= rsi <= self.rsi_max
        conditions['rsi_range'] = {
            'met': rsi_ok,
            'value': f"{rsi:.0f}",
            'detail': f"Range: {self.rsi_min}-{self.rsi_max}",
            'required': f"{self.rsi_min}-{self.rsi_max}"
        }
        
        # 3. RSI rising
        rsi_3ago = self.ai.rsi(closes[:-3]) if len(closes) > 17 else None
        rsi_rising = rsi_3ago is not None and rsi > rsi_3ago + 1.0
        conditions['rsi_rising'] = {
            'met': rsi_rising,
            'value': f"{'+' if rsi_rising else '−'}{abs(rsi - (rsi_3ago or rsi)):.1f}",
            'detail': f"3-bars ago: {rsi_3ago:.0f}" if rsi_3ago else "N/A",
            'required': "Rising ≥ 1 point"
        }
        
        # 4. MACD
        hist, phist = self.ai.macd_histogram(closes)
        macd_ok = hist > phist
        conditions['macd'] = {
            'met': macd_ok,
            'value': f"{hist:+.6f}",
            'detail': f"Prev: {phist:+.6f}",
            'required': "Improving"
        }
        
        # 5. Price vs EMA
        price_ok = cur >= ema20 * 0.93
        conditions['price_ema'] = {
            'met': price_ok,
            'value': f"{cur:.4f}",
            'detail': f"EMA20×0.93: {ema20*0.93:.4f}",
            'required': "≥ EMA20×0.93"
        }
        
        # 6. Volume
        vol_ma = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 1
        rel_vol = volumes[-1] / vol_ma if vol_ma > 0 else 1.0
        vol_ok = rel_vol >= self.vol_threshold
        conditions['volume'] = {
            'met': vol_ok,
            'value': f"{rel_vol:.2f}x",
            'detail': f"Threshold: {self.vol_threshold}x",
            'required': f"≥ {self.vol_threshold}x"
        }
        
        # Calculate score
        total_met = sum(1 for c in conditions.values() if c['met'])
        total_required = len(conditions)
        
        return {
            'ready': True,
            'signal': analysis.get('signal', 'HOLD'),
            'conditions': conditions,
            'score': f"{total_met}/{total_required}",
            'total_met': total_met,
            'total_required': total_required,
            'current_price': cur
        }
    
    def print_status(self):
        """Print detailed status for all tokens"""
        self.update_prices()
        
        print("\n" + "="*100)
        print(f"MARKET CONDITIONS — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Parameters: {'RELAXED' if self.relaxed else 'STANDARD'}")
        print("="*100)
        
        for sym in self.tokens:
            result = self.analyze_detailed(sym)
            
            if not result['ready']:
                print(f"\n🔄 {sym:<6} {result['reason']}")
                continue
            
            # Header
            met = result['total_met']
            total = result['total_required']
            signal = result['signal']
            price = result['current_price']
            
            # Status symbol
            if met == total:
                symbol = "🟢"
                status = "READY TO TRADE"
            elif met >= total - 1:
                symbol = "🟡"
                status = "ALMOST READY"
            elif met >= total - 2:
                symbol = "🟠"
                status = "NEEDS WORK"
            else:
                symbol = "🔴"
                status = "NOT READY"
            
            print(f"\n{symbol} {sym:<6} ${price:<10.6f}  [{result['score']}]  {signal:<5}  {status}")
            print("   " + "-"*90)
            
            # Conditions
            for name, cond in result['conditions'].items():
                check = "✓" if cond['met'] else "✗"
                value = cond['value']
                detail = cond['detail']
                
                if cond['met']:
                    print(f"   {check} {name:14}  {value:12}  {detail}")
                else:
                    print(f"   {check} {name:14}  {value:12}  {detail}  ← BLOCKING")
        
        print("\n" + "="*100)
    
    def run_continuous(self, interval: int = 60):
        """Run continuous monitoring"""
        self.initialize()
        
        print(f"\n✅ Monitor started. Refreshing every {interval}s. Press Ctrl+C to stop.\n")
        
        try:
            while True:
                self.print_status()
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n\n👋 Monitor stopped.")


def main():
    parser = argparse.ArgumentParser(description='Monitor trading conditions in real-time')
    parser.add_argument('--tokens', nargs='+', 
                       default=["SOL", "JUP", "RAY", "BONK", "WIF"],
                       help='Tokens to monitor')
    parser.add_argument('--relaxed', action='store_true',
                       help='Use relaxed parameters (RSI 38-68, lower thresholds)')
    parser.add_argument('--interval', type=int, default=60,
                       help='Update interval in seconds (default: 60)')
    parser.add_argument('--once', action='store_true',
                       help='Run once and exit (no continuous monitoring)')
    
    args = parser.parse_args()
    
    # Validate tokens
    valid_tokens = [t.upper() for t in args.tokens if t.upper() in TOKENS]
    if not valid_tokens:
        print("❌ No valid tokens specified")
        print(f"   Available: {', '.join(TOKENS.keys())}")
        sys.exit(1)
    
    monitor = MarketMonitor(valid_tokens, relaxed=args.relaxed)
    
    if args.once:
        monitor.initialize()
        monitor.print_status()
    else:
        monitor.run_continuous(interval=args.interval)


if __name__ == "__main__":
    main()
