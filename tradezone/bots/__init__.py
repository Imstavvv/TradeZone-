from .coach import TEAM_RULES, CoachBot
from .execution import ExecutionBot
from .executive import ExecutiveBot
from .grok import GrokBot
from .news import NewsBot
from .portfolio_manager import PortfolioBot
from .quant import QuantBot
from .risk import RiskBot
from .scout import ScoutBot
from .sentinel import SentinelBot
from .skimmer import SkimmerBot
from .strategist import StrategistBot

# Order matters only for on_cycle hooks: data -> analysis -> risk -> execution -> book -> audit
TEAM = [ScoutBot, NewsBot, StrategistBot, QuantBot, GrokBot, RiskBot, ExecutiveBot, ExecutionBot, PortfolioBot, SkimmerBot, SentinelBot,
        CoachBot]

__all__ = [c.__name__ for c in TEAM] + ["TEAM", "TEAM_RULES"]
