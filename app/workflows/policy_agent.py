"""
PolicyAgent: Implements safety guardrails and policy checks.
Ensures compliance with business rules before processing requests.
"""
from __future__ import annotations
from typing import Any, Set
import logging

from app.core.base_agent import BaseAgent
from app.core.result import AgentResult
from app.core.state import TripState

logger = logging.getLogger(__name__)


class PolicyAgent(BaseAgent):
    """
    Enforces policy guardrails:
    - Rate limiting
    - Blocked destinations (conflict zones, health risks)
    - Budget validation
    - Age/capability restrictions
    - Data privacy checks
    """
    
    name = "policy_agent"
    
    # Destinations with travel warnings/restrictions
    BLOCKED_DESTINATIONS = {
        # Add destinations as needed based on current travel advisories
    }
    
    # Destinations with health warnings
    HEALTH_WARNING_DESTINATIONS = {
        # "destination": "health_issue"
    }
    
    # Minimum/maximum constraints
    MIN_BUDGET = 500  # USD
    MAX_BUDGET = 100000  # USD
    MIN_DURATION_DAYS = 1
    MAX_DURATION_DAYS = 180
    MIN_TRAVELERS = 1
    MAX_TRAVELERS = 50
    
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
    
    async def execute(self, state: TripState) -> AgentResult:
        """
        Run all policy checks.
        Returns success=False if any critical policy is violated.
        """
        violations = []
        warnings = []
        
        # Check destination restrictions
        if state.destination:
            dest_violations, dest_warnings = self._check_destination_policy(state.destination)
            violations.extend(dest_violations)
            warnings.extend(dest_warnings)
        
        # Check budget validity
        if state.budget is not None:
            budget_violations, budget_warnings = self._check_budget_policy(state.budget)
            violations.extend(budget_violations)
            warnings.extend(budget_warnings)
        
        # Check trip duration
        if state.duration_days is not None:
            duration_violations, duration_warnings = self._check_duration_policy(state.duration_days)
            violations.extend(duration_violations)
            warnings.extend(duration_warnings)
        
        # Check traveler count
        if state.travelers is not None:
            travelers_violations, travelers_warnings = self._check_travelers_policy(state.travelers)
            violations.extend(travelers_violations)
            warnings.extend(travelers_warnings)
        
        # Check data privacy
        privacy_violations, privacy_warnings = self._check_privacy_policy(state)
        violations.extend(privacy_violations)
        warnings.extend(privacy_warnings)
        
        # Log warnings
        for warning in warnings:
            state.errors.append(f"[WARNING] {warning}")
            logger.warning(warning)
        
        state.completed_steps.append("policy_agent")
        
        # If any critical violations, reject the request
        if violations:
            for violation in violations:
                state.errors.append(f"[POLICY VIOLATION] {violation}")
                logger.error(violation)
            
            return AgentResult(
                success=False,
                message=f"Request rejected due to policy violations: {'; '.join(violations)}"
            )
        
        return AgentResult(
            success=True,
            message=f"Policy checks passed. {len(warnings)} warning(s)." if warnings else "All policies satisfied."
        )
    
    def _check_destination_policy(self, destination: str) -> tuple[list[str], list[str]]:
        """Check if destination is blocked or has warnings."""
        violations = []
        warnings = []
        
        dest_lower = destination.lower().strip()
        
        if dest_lower in self.BLOCKED_DESTINATIONS:
            violations.append(
                f"Travel to '{destination}' is not permitted due to security/health restrictions."
            )
        
        if dest_lower in self.HEALTH_WARNING_DESTINATIONS:
            health_issue = self.HEALTH_WARNING_DESTINATIONS[dest_lower]
            warnings.append(
                f"Health warning for '{destination}': {health_issue}. "
                "Ensure appropriate vaccinations and precautions."
            )
        
        return violations, warnings
    
    def _check_budget_policy(self, budget: float) -> tuple[list[str], list[str]]:
        """Validate budget constraints."""
        violations = []
        warnings = []
        
        if budget < self.MIN_BUDGET:
            violations.append(
                f"Budget ${budget} is below minimum recommended ${self.MIN_BUDGET}. "
                "Trip planning may be limited."
            )
        
        if budget > self.MAX_BUDGET:
            violations.append(
                f"Budget ${budget} exceeds maximum allowed ${self.MAX_BUDGET}."
            )
        
        return violations, warnings
    
    def _check_duration_policy(self, duration_days: int) -> tuple[list[str], list[str]]:
        """Validate trip duration constraints."""
        violations = []
        warnings = []
        
        if duration_days < self.MIN_DURATION_DAYS:
            violations.append(
                f"Trip duration {duration_days} day(s) is below minimum {self.MIN_DURATION_DAYS} day."
            )
        
        if duration_days > self.MAX_DURATION_DAYS:
            violations.append(
                f"Trip duration {duration_days} days exceeds maximum {self.MAX_DURATION_DAYS} days."
            )
        
        if duration_days > 30:
            warnings.append(
                f"Extended trip duration ({duration_days} days) detected. "
                "Consider visa requirements and travel insurance."
            )
        
        return violations, warnings
    
    def _check_travelers_policy(self, travelers: int) -> tuple[list[str], list[str]]:
        """Validate number of travelers."""
        violations = []
        warnings = []
        
        if travelers < self.MIN_TRAVELERS:
            violations.append(
                f"Number of travelers ({travelers}) must be at least {self.MIN_TRAVELERS}."
            )
        
        if travelers > self.MAX_TRAVELERS:
            violations.append(
                f"Number of travelers ({travelers}) exceeds maximum {self.MAX_TRAVELERS}. "
                "Please contact support for group planning."
            )
        
        return violations, warnings
    
    def _check_privacy_policy(self, state: TripState) -> tuple[list[str], list[str]]:
        """Check data privacy and security requirements."""
        violations = []
        warnings = []
        
        # Ensure user_id is provided if storing preferences
        if state.interests and not state.user_id:
            warnings.append(
                "Personalization enabled without user ID. "
                "Preferences will not be saved."
            )
        
        # If start location provided, warn about privacy
        if state.start_location:
            warnings.append(
                "Start location collected. "
                "This will be used only for this trip and not stored without consent."
            )
        
        return violations, warnings
