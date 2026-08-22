#!/usr/bin/env python3
"""
Market Condition Analyzer
Determines if market conditions are suitable for trend-following strategy
Auto-pauses trading when conditions are poor
"""

from typing import Dict, List, Tuple
from datetime import datetime


class MarketConditionAnalyzer:
    """
    Analyzes overall market health and determines if trading should proceed.
    
    Scores market on multiple factors:
    - Trend strength (how many tokens are in clear trends)
    - Volatility (sufficient movement for profit)
    - Volume (market participation)
    - Momentum consistency
    
    Returns:
    - HEALTHY: Good conditions, trade normally
    - MARGINAL: Borderline, trade with caution
    - POOR: Bad conditions, pause trading
    """
    
    def __init__(self):
        self.history = []  # Track condition assessments over time
        self.pause_count = 0
        self.resume_count = 0
    
    def analyze(self, tokens_analysis: Dict[str, Dict]) -> Tuple[str, str, Dict]:
        """
        Analyze market conditions across all tokens.
        
        Args:
            tokens_analysis: Dict of {token: analysis_result} from TradingAI
        
        Returns:
            (status, reason, details) where:
            - status: "HEALTHY", "MARGINAL", or "POOR"
            - reason: Human-readable explanation
            - details: Dict with scoring breakdown
        """
        if not tokens_analysis:
            return "POOR", "No token data available", {}
        
        scores = {
            'trend_quality': 0,
            'volatility': 0,
            'momentum': 0,
            'volume': 0
        }
        
        total_tokens = len(tokens_analysis)
        
        # 1. Trend Quality Score (0-100)
        bullish_count = sum(1 for a in tokens_analysis.values() 
                           if a.get('trend') == 'BULLISH')
        bearish_count = sum(1 for a in tokens_analysis.values() 
                           if a.get('trend') == 'BEARISH')
        neutral_count = total_tokens - bullish_count - bearish_count
        
        # Strong directional markets are good
        directional_pct = (bullish_count + bearish_count) / total_tokens
        scores['trend_quality'] = int(directional_pct * 100)
        
        # Bonus for predominantly bullish (preferred for longs)
        if bullish_count > bearish_count:
            scores['trend_quality'] += 10
        
        scores['trend_quality'] = min(100, scores['trend_quality'])
        
        # 2. Volatility Score (0-100)
        # Check EMA slopes - need sufficient movement
        slopes = [abs(a.get('ema_slope', 0)) for a in tokens_analysis.values()]
        avg_slope = sum(slopes) / len(slopes) if slopes else 0
        
        # Ideal slope: 0.002-0.010 (0.2%-1.0%)
        if avg_slope < 0.0005:
            scores['volatility'] = 20  # Too flat
        elif avg_slope < 0.001:
            scores['volatility'] = 40
        elif avg_slope < 0.003:
            scores['volatility'] = 80  # Good range
        elif avg_slope < 0.010:
            scores['volatility'] = 100  # Ideal
        else:
            scores['volatility'] = 60  # Too volatile/choppy
        
        # 3. Momentum Score (0-100)
        # Check RSI distribution and MACD
        rsi_values = [a.get('rsi', 50) for a in tokens_analysis.values()]
        avg_rsi = sum(rsi_values) / len(rsi_values) if rsi_values else 50
        
        # RSI should be in tradeable range (not extreme)
        if 40 <= avg_rsi <= 60:
            scores['momentum'] = 100  # Ideal - room to move both ways
        elif 30 <= avg_rsi <= 70:
            scores['momentum'] = 70  # Acceptable
        else:
            scores['momentum'] = 30  # Too extreme
        
        # Check MACD histogram improving count
        improving_count = sum(1 for a in tokens_analysis.values()
                             if 'MACD↑' in a.get('reason', ''))
        improving_pct = improving_count / total_tokens
        scores['momentum'] += int(improving_pct * 20)
        scores['momentum'] = min(100, scores['momentum'])
        
        # 4. Volume Score (0-100)
        # This is harder to assess without actual volume data
        # For now, assume moderate score
        scores['volume'] = 60
        
        # Calculate overall health score (weighted average)
        weights = {
            'trend_quality': 0.40,  # Most important
            'volatility': 0.30,     # Second most important
            'momentum': 0.20,
            'volume': 0.10
        }
        
        overall_score = sum(scores[k] * weights[k] for k in scores)
        
        # Determine status
        if overall_score >= 70:
            status = "HEALTHY"
            reason = f"Good market conditions (score: {overall_score:.0f}/100) - trading enabled"
        elif overall_score >= 50:
            status = "MARGINAL"
            reason = f"Borderline conditions (score: {overall_score:.0f}/100) - trading paused (require 70+)"
        else:
            status = "POOR"
            reason = f"Poor market conditions (score: {overall_score:.0f}/100) - trading paused"
        
        # Add specific reasons
        issues = []
        if scores['trend_quality'] < 40:
            issues.append(f"{neutral_count}/{total_tokens} tokens in neutral trend")
        if scores['volatility'] < 40:
            issues.append(f"low volatility (avg slope: {avg_slope:.4f})")
        if scores['momentum'] < 40:
            issues.append(f"extreme RSI (avg: {avg_rsi:.0f})")
        
        if issues:
            reason += " — " + "; ".join(issues)
        
        details = {
            'overall_score': overall_score,
            'scores': scores,
            'stats': {
                'bullish': bullish_count,
                'bearish': bearish_count,
                'neutral': neutral_count,
                'avg_rsi': avg_rsi,
                'avg_slope': avg_slope
            }
        }
        
        # Track history
        self.history.append({
            'timestamp': datetime.now(),
            'status': status,
            'score': overall_score
        })
        
        # Keep only last 100 assessments
        if len(self.history) > 100:
            self.history.pop(0)
        
        return status, reason, details
    
    def should_trade(self, tokens_analysis: Dict[str, Dict], 
                     min_score: int = 70) -> Tuple[bool, str]:
        """
        Simple yes/no: Should we trade right now?
        
        Args:
            tokens_analysis: Dict of token analyses
            min_score: Minimum score required (default 70 for HEALTHY only)
        
        Returns:
            (should_trade, reason)
        """
        status, reason, details = self.analyze(tokens_analysis)
        
        score = details.get('overall_score', 0)
        should_trade = score >= min_score
        
        if should_trade:
            return True, f"✓ Trading enabled ({status})"
        else:
            return False, f"⏸ Trading paused ({status}): {reason}"
    
    def get_recent_trend(self, lookback: int = 10) -> str:
        """
        Check if conditions are improving or degrading.
        
        Returns:
            "IMPROVING", "STABLE", or "DEGRADING"
        """
        if len(self.history) < lookback:
            return "STABLE"
        
        recent = self.history[-lookback:]
        scores = [h['score'] for h in recent]
        
        # Linear regression on recent scores
        x = list(range(len(scores)))
        n = len(scores)
        
        sum_x = sum(x)
        sum_y = sum(scores)
        sum_xy = sum(x[i] * scores[i] for i in range(n))
        sum_x2 = sum(xi * xi for xi in x)
        
        slope = (n * sum_xy - sum_x * sum_y) / (n * sum_x2 - sum_x * sum_x)
        
        if slope > 2:
            return "IMPROVING"
        elif slope < -2:
            return "DEGRADING"
        else:
            return "STABLE"
    
    def print_status(self, tokens_analysis: Dict[str, Dict]):
        """Print detailed status report"""
        status, reason, details = self.analyze(tokens_analysis)
        
        score = details['overall_score']
        scores = details['scores']
        stats = details['stats']
        
        # Status emoji
        if status == "HEALTHY":
            emoji = "🟢"
        elif status == "MARGINAL":
            emoji = "🟡"
        else:
            emoji = "🔴"
        
        print("\n" + "="*70)
        print(f"{emoji} MARKET CONDITION: {status} (Score: {score:.0f}/100)")
        print("="*70)
        
        print(f"\nBreakdown:")
        print(f"  Trend Quality: {scores['trend_quality']:>3}/100  "
              f"({stats['bullish']} bullish, {stats['bearish']} bearish, "
              f"{stats['neutral']} neutral)")
        print(f"  Volatility:    {scores['volatility']:>3}/100  "
              f"(avg slope: {stats['avg_slope']:+.4f})")
        print(f"  Momentum:      {scores['momentum']:>3}/100  "
              f"(avg RSI: {stats['avg_rsi']:.0f})")
        print(f"  Volume:        {scores['volume']:>3}/100")
        
        print(f"\n{reason}")
        
        if status == "POOR":
            print("\n⏸  TRADING PAUSED - Waiting for better conditions...")
            print("   Bot will auto-resume when market score reaches 70+")
        elif status == "MARGINAL":
            print("\n⏸  TRADING PAUSED - Market conditions not strong enough")
            print("   Require score 70+ (HEALTHY) to trade")
            print("   Bot will auto-resume when conditions improve")
        else:
            print("\n✅ TRADING ENABLED - Market conditions are good")
            print("   Will pause automatically if score drops below 70")
        
        print("="*70)


def test_analyzer():
    """Test the analyzer with sample data"""
    analyzer = MarketConditionAnalyzer()
    
    # Simulate poor conditions (all neutral)
    poor_analysis = {
        'SOL': {'trend': 'NEUTRAL', 'rsi': 50, 'ema_slope': 0.0002, 'reason': ''},
        'JUP': {'trend': 'NEUTRAL', 'rsi': 48, 'ema_slope': 0.0001, 'reason': ''},
        'RAY': {'trend': 'NEUTRAL', 'rsi': 52, 'ema_slope': -0.0001, 'reason': ''},
        'BONK': {'trend': 'NEUTRAL', 'rsi': 51, 'ema_slope': 0.0000, 'reason': ''},
        'WIF': {'trend': 'NEUTRAL', 'rsi': 49, 'ema_slope': 0.0003, 'reason': ''},
    }
    
    print("TEST 1: Poor Market Conditions")
    analyzer.print_status(poor_analysis)
    
    # Simulate healthy conditions (mostly bullish with good momentum)
    healthy_analysis = {
        'SOL': {'trend': 'BULLISH', 'rsi': 55, 'ema_slope': 0.0035, 'reason': 'MACD↑'},
        'JUP': {'trend': 'BULLISH', 'rsi': 48, 'ema_slope': 0.0028, 'reason': 'MACD↑'},
        'RAY': {'trend': 'BEARISH', 'rsi': 45, 'ema_slope': -0.0025, 'reason': ''},
        'BONK': {'trend': 'BULLISH', 'rsi': 52, 'ema_slope': 0.0031, 'reason': 'MACD↑'},
        'WIF': {'trend': 'BULLISH', 'rsi': 58, 'ema_slope': 0.0040, 'reason': 'MACD↑'},
    }
    
    print("\n\nTEST 2: Healthy Market Conditions")
    analyzer.print_status(healthy_analysis)
    
    # Test should_trade
    should, reason = analyzer.should_trade(poor_analysis)
    print(f"\n\nPoor market - Should trade? {should} - {reason}")
    
    should, reason = analyzer.should_trade(healthy_analysis)
    print(f"Healthy market - Should trade? {should} - {reason}")


if __name__ == "__main__":
    test_analyzer()
