# app/core/result.py
from typing import Optional
from pydantic import BaseModel
from app.core.state import TripState

class AgentResult(BaseModel):
    success: bool
    state: TripState
    error: Optional[str] = None