# Now every future agent must implement async def execute(...)
from abc import ABC, abstractmethod
from typing import Any

from app.core.state import TripState
from app.core.result import AgentResult


class BaseAgent(ABC):
    name: str = "base_agent"

    def __init__(self, **kwargs: Any) -> None:
        # Subclasses call super().__init__(**kwargs); object.__init__ errors
        # if given any args/kwargs, so this exists to absorb them safely.
        super().__init__()

    @abstractmethod
    async def execute(self, state: TripState) -> AgentResult:
        pass