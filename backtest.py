#!/usr/bin/env python3
"""
Backtesting framework for Solana trading bot.
Allows testing of strategy parameters on historical data.
"""

import json
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Callable
import pandas as pd
import numpy as np
import requests

from solana_trading_agent import SolanaTradingAgent, TradingMode, TOKENS, TradingAI
from trending_tokens import TrendingTokenDetector


class Backtester:
    """
    Backtester for Solana trading strategies.
    
    Usage:
        bt = Backtester(initial_balance=1000.0)
        bt.load_historical_data("SOL", days=30)
        bt.set_strategy_params(stop_loss=0.05, take_profit=0.15)
        bt.run(start_date=datetime(2025,1,1), end_date=datetime(2025,1,31))
        bt.print_results()
    """
    
    def __init__(self, initial_balance: float = 1000.0):
        self.agent = SolanaTradingAgent(mode=TradingMode.PAPER, initial_usdc=initial_balance)
        self.detector = TrendingTokenDetector()
        self.historical_data: Dict[str, pd.DataFrame] = {}  # token -> DataFrame with 'timestamp', 'close', 'volume'
        
        # Strategy parameters (defaults)
        self.stop_loss_pct = 0.05
        self.take_profit_pct = 0.15
        self.min_confidence = 0.55
        self.max_position_pct = 0.10
        
        # Tracking
        self.trades: List[Dict] = []
        self.equity_curve: List[Dict] = []
    
    def load_historical_data(self, token_symbol: str, days: int = 30, source: str = "dexscreener"):
        """
        Load historical OHLCV data for a token.
        
        Args:
            token_symbol: Token symbol (e.g., "SOL")
            days: Number of days of history to fetch
            source: Data source ("dexscreener" or "geckoterminal")
        """
        token_mint = TOKENS.get(token_symbol)
        if not token_mint:
            print(f"Unknown token: {token_symbol}")
            return
        
        print(f"Loading {days} days of historical data for {token_symbol}...")
        
        # For demonstration, we'll fetch from DexScreener (1-hour candles via GeckoTerminal)
        # In practice, you might use a proper OHLCV API.
        # This is a simplified placeholder.
        
        # Example: use GeckoTerminal OHLCV endpoint
        # We'll need the pair address; for simplicity, we assume we have a mapping.
        # Since this is a framework, we'll simulate a simple price series.
        
        # For now, generate synthetic historical data (replace with real fetch)
        np.random.seed(42)
        dates = pd.date_range(end=datetime.now(), periods=days*24, freq='h')
        prices = 100 * np.exp(np.cumsum(np.random.randn(len(dates)) * 0.02))
        volumes = np.random.randint(100000, 10000000, size=len(dates))
        
        df = pd.DataFrame({
            'timestamp': dates,
            'close': prices,
            'volume': volumes
        })
        df.set_index('timestamp', inplace=True)
        
        self.historical_data[token_symbol] = df
        print(f"✓ Loaded {len(df)} candles for {token_symbol}")
    
    def set_strategy_params(self, **kwargs):
        """Set strategy parameters."""
        self.stop_loss_pct = kwargs.get('stop_loss', self.stop_loss_pct)
        self.take_profit_pct = kwargs.get('take_profit', self.take_profit_pct)
        self.min_confidence = kwargs.get('min_confidence', self.min_confidence)
        self.max_position_pct = kwargs.get('max_position', self.max_position_pct)
    
    def run(self, start_date: datetime, end_date: datetime, interval_minutes: int = 60):
        """
        Run backtest over date range.
        
        Args:
            start_date: Start datetime
            end_date: End datetime
            interval_minutes: Time step in minutes
        """
        print(f"\nRunning backtest from {start_date} to {end_date}...")
        
        current = start_date
        step = timedelta(minutes=interval_minutes)
        
        # Reset agent portfolio
        self.agent.portfolio = SolanaPortfolio(self.agent.portfolio.initial_usdc)
        self.agent.price_history = {}
        
        # Add tokens to watchlist
        for token in self.historical_data.keys():
            self.agent.add_token(token)
        
        # Main simulation loop
        while current <= end_date:
            # Update market data for each token at this timestamp
            for token, df in self.historical_data.items():
                # Find closest candle to current timestamp
                idx = df.index.get_indexer([current], method='nearest')[0]
                if idx < 0 or idx >= len(df):
                    continue
                row = df.iloc[idx]
                price = row['close']
                volume = row['volume']
                
                # Update agent's price history
                if token not in self.agent.price_history:
                    self.agent.price_history[token] = {"prices": [], "volumes": []}
                self.agent.price_history[token]["prices"].append(price)
                self.agent.price_history[token]["volumes"].append(volume)
                
                # Keep only last 1000
                if len(self.agent.price_history[token]["prices"]) > 1000:
                    self.agent.price_history[token]["prices"].pop(0)
                    self.agent.price_history[token]["volumes"].pop(0)
                
                # Update portfolio prices
                self.agent.portfolio.update_prices({token: price})
            
            # Run trading logic for this tick
            self._run_trading_cycle()
            
            # Record equity
            self.equity_curve.append({
                'timestamp': current,
                'value': self.agent.portfolio.get_total_value()
            })
            
            current += step
        
        print("Backtest complete.\n")
    
    def _run_trading_cycle(self):
        """Simulate one cycle of trading decisions."""
        for token in self.agent.watched_tokens:
            if token == "USDC":
                continue
            
            analysis = self.agent.analyze_token(token)
            if not analysis or analysis['signal'] not in ['BUY', 'SELL']:
                continue
            
            if analysis['signal'] == 'BUY':
                # Check if we already hold
                if token in self.agent.portfolio.positions:
                    continue
                
                # Check confidence
                if analysis['confidence'] < self.min_confidence:
                    continue
                
                # Calculate position size
                total_value = self.agent.portfolio.get_total_value()
                usdc_amount = min(total_value * self.max_position_pct,
                                  self.agent.portfolio.usdc_balance * 0.95)
                if usdc_amount < 10:
                    continue
                
                # Execute buy
                trade = self.agent.execute_swap("USDC", token, usdc_amount)
                if trade:
                    self.trades.append({
                        'timestamp': datetime.now(),
                        'token': token,
                        'side': 'BUY',
                        'price': analysis['current_price'],
                        'amount': usdc_amount / analysis['current_price'],
                        'usdc_amount': usdc_amount
                    })
            
            elif analysis['signal'] == 'SELL':
                if token not in self.agent.portfolio.positions:
                    continue
                
                # Check confidence
                if analysis['confidence'] < self.min_confidence:
                    continue
                
                position = self.agent.portfolio.positions[token]
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.trades.append({
                        'timestamp': datetime.now(),
                        'token': token,
                        'side': 'SELL',
                        'price': analysis['current_price'],
                        'amount': position.amount,
                        'usdc_amount': position.amount * analysis['current_price']
                    })
    
    def print_results(self):
        """Print backtest performance metrics."""
        perf = self.agent.portfolio.get_performance()
        
        print("="*60)
        print("BACKTEST RESULTS")
        print("="*60)
        print(f"Initial Balance: ${perf['initial_balance']:.2f}")
        print(f"Final Balance:   ${perf['current_value']:.2f}")
        print(f"Total Return:    ${perf['total_return']:.2f} ({perf['total_return_pct']:.2f}%)")
        print(f"Total Trades:    {perf['total_trades']}")
        
        # Additional metrics
        if len(self.trades) > 0:
            buy_trades = [t for t in self.trades if t['side'] == 'BUY']
            sell_trades = [t for t in self.trades if t['side'] == 'SELL']
            completed = min(len(buy_trades), len(sell_trades))
            if completed > 0:
                # Simple win/loss (for demo, not accurate without tracking entry/exit pairs)
                print(f"Completed Round Trips: {completed}")
        
        # Drawdown
        if self.equity_curve:
            values = [e['value'] for e in self.equity_curve]
            peak = np.maximum.accumulate(values)
            drawdown = (peak - values) / peak * 100
            max_dd = np.max(drawdown)
            print(f"Max Drawdown:      {max_dd:.2f}%")
        
        print("="*60)
    
    def plot_equity_curve(self):
        """Plot equity curve (requires matplotlib)."""
        try:
            import matplotlib.pyplot as plt
            df = pd.DataFrame(self.equity_curve)
            df.set_index('timestamp', inplace=True)
            df['value'].plot(title='Equity Curve', figsize=(12,6))
            plt.ylabel('Portfolio Value ($)')
            plt.grid(True)
            plt.show()
        except ImportError:
            print("matplotlib not installed, cannot plot.")


def example_usage():
    """Example of how to use the backtester."""
    bt = Backtester(initial_balance=1000.0)
    
    # Load historical data for a few tokens
    bt.load_historical_data("SOL", days=30)
    bt.load_historical_data("JUP", days=30)
    
    # Set strategy parameters
    bt.set_strategy_params(
        stop_loss=0.05,
        take_profit=0.15,
        min_confidence=0.55,
        max_position=0.10
    )
    
    # Run backtest
    end = datetime.now()
    start = end - timedelta(days=7)
    bt.run(start_date=start, end_date=end, interval_minutes=60)
    
    # Print results
    bt.print_results()
    
    # Plot equity curve
    bt.plot_equity_curve()


if __name__ == "__main__":
    example_usage()