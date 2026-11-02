from .execution import ExecutionBot
from .executive import ExecutiveBot
from .grok import GrokBot
from .portfolio_manager import PortfolioBot
from .quant import QuantBot
from .risk import RiskBot
from .scout import ScoutBot
from .sentinel import SentinelBot
from .strategist import StrategistBot

# Order matters only for on_cycle hooks: data -> analysis -> risk -> execution -> book -> audit
TEAM = [ScoutBot, StrategistBot, QuantBot, GrokBot, RiskBot, ExecutiveBot, ExecutionBot, PortfolioBot, SentinelBot]

__all__ = [c.__name__ for c in TEAM] + ["TEAM"]
