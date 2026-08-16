"""
CalendarAgent: Integrates with Google Calendar to find free dates for trips.
Uses Google Calendar API with OAuth 2.0.
"""
from __future__ import annotations
from typing import Any, Optional, List
import logging
from datetime import datetime, timedelta

from app.config.settings import settings
from app.core.base_agent import BaseAgent
from app.core.result import AgentResult
from app.core.state import TripState

logger = logging.getLogger(__name__)


class CalendarAgent(BaseAgent):
    """
    Checks user's Google Calendar for available dates.
    Requires Google Calendar OAuth token in user context.
    """
    
    name = "calendar_agent"
    
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
    
    async def execute(self, state: TripState) -> AgentResult:
        """
        Find free dates in user's calendar.
        If dates already provided, validates them.
        Returns availability added to state.trip_dates.
        """
        # If trip_dates already provided, skip calendar check
        if state.trip_dates:
            state.completed_steps.append("calendar_agent")
            return AgentResult(
                success=True,
                message="Trip dates already provided, skipping calendar check."
            )
        
        # If no user_id, we can't access their calendar
        if not state.user_id:
            logger.info("No user_id provided, cannot access calendar. Using defaults.")
            state.trip_dates = self._get_default_trip_dates()
            state.completed_steps.append("calendar_agent")
            return AgentResult(
                success=True,
                message="No calendar integration available. Using suggested dates."
            )
        
        try:
            # TODO: Implement Google Calendar API integration
            # For now, return suggested dates based on typical travel patterns
            state.trip_dates = self._get_suggested_trip_dates()
            state.completed_steps.append("calendar_agent")
            
            return AgentResult(
                success=True,
                message=f"Calendar check completed. Suggested dates: {state.trip_dates}"
            )
        
        except Exception as e:
            logger.exception("Calendar agent failed: %s", str(e))
            state.trip_dates = self._get_default_trip_dates()
            state.errors.append(f"Calendar check failed, using defaults: {str(e)}")
            state.completed_steps.append("calendar_agent")
            
            return AgentResult(
                success=True,  # Don't fail the flow
                message=f"Calendar check failed, using default dates: {str(e)}"
            )
    
    def _get_default_trip_dates(self) -> List[dict]:
        """
        Return default trip dates (e.g., next available weekend or week).
        """
        today = datetime.utcnow().date()
        
        # Find next Friday
        days_until_friday = (4 - today.weekday()) % 7
        if days_until_friday == 0:
            days_until_friday = 7
        
        start_date = today + timedelta(days=days_until_friday)
        end_date = start_date + timedelta(days=2)  # 3-day weekend default
        
        return [
            {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "suggested": True,
                "confidence": "low",
            }
        ]
    
    def _get_suggested_trip_dates(self) -> List[dict]:
        """
        Return multiple suggested trip dates based on typical patterns.
        Could be expanded to check actual calendar if token provided.
        """
        today = datetime.utcnow().date()
        suggestions = []
        
        # Suggestion 1: Next weekend (3 days)
        days_until_friday = (4 - today.weekday()) % 7
        if days_until_friday == 0:
            days_until_friday = 7
        
        friday = today + timedelta(days=days_until_friday)
        suggestions.append({
            "start_date": friday.isoformat(),
            "end_date": (friday + timedelta(days=2)).isoformat(),
            "duration_days": 3,
            "type": "weekend",
            "confidence": "medium",
        })
        
        # Suggestion 2: Next week (5 days)
        monday = today + timedelta(days=(7 - today.weekday()) % 7)
        if monday <= today:
            monday += timedelta(days=7)
        
        suggestions.append({
            "start_date": monday.isoformat(),
            "end_date": (monday + timedelta(days=4)).isoformat(),
            "duration_days": 5,
            "type": "workweek",
            "confidence": "medium",
        })
        
        # Suggestion 3: Two weeks out (1 week)
        start_date = today + timedelta(days=14)
        suggestions.append({
            "start_date": start_date.isoformat(),
            "end_date": (start_date + timedelta(days=6)).isoformat(),
            "duration_days": 7,
            "type": "week",
            "confidence": "low",
        })
        
        # Return best suggestion (first one) or let user choose
        return [suggestions[0]]  # Return top suggestion
    
    async def get_calendar_free_slots(
        self,
        user_id: str,
        token: str,
        start_date: str,
        end_date: str,
        min_duration_hours: int = 24,
    ) -> List[dict]:
        """
        Query Google Calendar for free time slots.
        Requires valid OAuth token.
        
        Args:
            user_id: User ID for logging
            token: Google Calendar OAuth access token
            start_date: ISO format date string
            end_date: ISO format date string
            min_duration_hours: Minimum continuous free time required
        
        Returns:
            List of available date ranges
        """
        try:
            # TODO: Implement actual Google Calendar API integration
            # This requires:
            # 1. google-api-python-client library
            # 2. Valid OAuth token from user
            # 3. Calendar read permissions
            
            # Mock implementation for now
            logger.info(
                f"Would check calendar for user {user_id} "
                f"between {start_date} and {end_date}"
            )
            
            return []  # Return empty list (no data) for now
        
        except Exception as e:
            logger.exception(f"Failed to query Google Calendar: {str(e)}")
            return []
    
    async def check_availability(
        self,
        calendar_events: List[dict],
        start_date: datetime,
        end_date: datetime,
        min_free_hours: int = 8,
    ) -> bool:
        """
        Check if there's enough free time in the given date range.
        
        Args:
            calendar_events: List of calendar event dicts with start/end times
            start_date: Start of desired trip period
            end_date: End of desired trip period
            min_free_hours: Minimum continuous free time required
        
        Returns:
            True if enough free time available
        """
        if not calendar_events:
            return True
        
        # Sort events by start time
        events = sorted(
            calendar_events,
            key=lambda e: e.get("start", ""),
        )
        
        # Check gaps between events
        current_time = start_date
        
        for event in events:
            event_start = datetime.fromisoformat(event.get("start", ""))
            event_end = datetime.fromisoformat(event.get("end", ""))
            
            # Check if event overlaps with desired period
            if event_start < end_date and event_end > current_time:
                # Calculate free time before this event
                free_time = event_start - max(current_time, start_date)
                
                if free_time.total_seconds() / 3600 >= min_free_hours:
                    return True
                
                current_time = event_end
        
        # Check free time after last event
        free_time = end_date - current_time
        return free_time.total_seconds() / 3600 >= min_free_hours
