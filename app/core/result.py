# app/core/result.py
from typing import Optional
from pydantic import BaseModel
from app.core.state import TripState

class AgentResult(BaseModel):
    success: bool
    # Agents mutate the TripState instance they're given in place, so
    # returning it again here is optional -- kept for cases where a caller
    # wants the result to carry its own copy.
    state: Optional[TripState] = None
    message: Optional[str] = None
    error: Optional[str] = None