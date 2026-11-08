from .advisors import (
    CorrelationBot,
    DipBuyerBot,
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
from .chartvision import ChartVisionBot
from .coach import TEAM_RULES, CoachBot
from .debate import DebateBot
from .execution import ExecutionBot
from .executive import ExecutiveBot
from .flipper import FlipperBot
from .grok import GrokBot
from .learners import TargetLearnerBot, TradeReviewerBot
from .listings import ListingsBot
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
from .rerouter import RerouterBot
from .risk import RiskBot
from .rugguard import RugGuardBot
from .scout import ScoutBot
from .sentinel import SentinelBot
from .skimmer import SkimmerBot
from .smartmoney import SmartMoneyBot
from .stacker import StackerBot
from .strategist import StrategistBot
from .trends import TrendBot

# Order matters only for on_cycle hooks: data -> analysis -> risk -> execution -> book -> audit
TEAM = [ScoutBot, ListingsBot, TrendBot, NewsBot, StrategistBot,
        # advisors the Risk Manager consults before every new trade
        WeatherBot, VolSizerBot, RankerBot, StretchBot, LevelsBot, CorrelationBot, WhaleBot, SmartMoneyBot, ChartVisionBot, DebateBot, LiquidityBot,
        ShockGuardBot, HTFBot,
        QuantBot, PullbackBot, GrokBot, RiskBot, ExecutiveBot, ExecutionBot, RerouterBot, PortfolioBot, RugGuardBot, SkimmerBot, FlipperBot, DipBuyerBot, StackerBot,
        FeeAuditorBot, SlippageBot, UptimeBot, AccountantBot, SpeedBot, HousekeeperBot, SentinelBot, CoachBot,
        TargetLearnerBot, TradeReviewerBot]

__all__ = [c.__name__ for c in TEAM] + ["TEAM", "TEAM_RULES"]
