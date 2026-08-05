# Now every future agent must implement async def execute(...)
from abc import ABC, abstractmethod

from app.core.state import TripState
from app.core.result import AgentResult


class BaseAgent(ABC):

    @abstractmethod
    async def execute(self, state: TripState) -> AgentResult:
        pass