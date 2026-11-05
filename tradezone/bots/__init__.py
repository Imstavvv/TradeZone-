from .advisors import (
    CorrelationBot,
    HTFBot,
    LevelsBot,
    LiquidityBot,
    PullbackBot,
    RankerBot,
    ShockGuardBot,
    StretchBot,
    VolSizerBot,
    WeatherBot,
    WhaleBot,
)
from .coach import TEAM_RULES, CoachBot
from .execution import ExecutionBot
from .executive import ExecutiveBot
from .flipper import FlipperBot
from .grok import GrokBot
from .news import NewsBot
from .ops import (
    AccountantBot,
    FeeAuditorBot,
    HousekeeperBot,
    SlippageBot,
    SpeedBot,
    UptimeBot,
)
from .portfolio_manager import PortfolioBot
from .quant import QuantBot
from .risk import RiskBot
from .scout import ScoutBot
from .sentinel import SentinelBot
from .skimmer import SkimmerBot
from .stacker import StackerBot
from .strategist import StrategistBot
from .trends import TrendBot

# Order matters only for on_cycle hooks: data -> analysis -> risk -> execution -> book -> audit
TEAM = [ScoutBot, TrendBot, NewsBot, StrategistBot,
        # advisors the Risk Manager consults before every new trade
        WeatherBot, VolSizerBot, RankerBot, StretchBot, LevelsBot, CorrelationBot, WhaleBot, LiquidityBot,
        ShockGuardBot, HTFBot,
        QuantBot, PullbackBot, GrokBot, RiskBot, ExecutiveBot, ExecutionBot, PortfolioBot, SkimmerBot, FlipperBot, StackerBot,
        FeeAuditorBot, SlippageBot, UptimeBot, AccountantBot, SpeedBot, HousekeeperBot, SentinelBot, CoachBot]

__all__ = [c.__name__ for c in TEAM] + ["TEAM", "TEAM_RULES"]
