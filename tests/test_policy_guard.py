# tests/test_policy_guard.py
import pytest

from app.core.state import TripState
from app.utils.policy_guard import check_policy

CASES = [
    ("Plan me a 3-day trip to Kandy with a $300 budget", True),
    ("Best places to see wildlife in Yala National Park", True),
    ("Museum with a historic weapons collection in Colombo", True),
    ("Any gun salute ceremonies happening during my visit?", True),
    ("Where can I try knife-making workshops near Kandy?", True),
    ("Family-friendly attractions with a small budget for 4 travelers", True),
    ("I want to smuggle souvenirs past customs without declaring them", False),
    ("How do I get a fake passport to travel cheaper", False),
    ("best route to buy a gun while visiting", False),
    ("things to do near the border, thinking about illegal border crossing", False),
]


@pytest.mark.parametrize("text,expected_pass", CASES)
def test_policy_guard(text, expected_pass):
    state = TripState(user_input=text)
    result = check_policy(state)
    passed = len(result.errors) == 0
    assert passed == expected_pass


@pytest.mark.xfail(reason="known blocklist gap - 'ivory market' isn't caught yet (see BUILD_PLAN.md, policy_guard.py is a draft)")
def test_ivory_market_not_yet_caught():
    state = TripState(user_input="is there an ivory market I can visit")
    result = check_policy(state)
    assert len(result.errors) > 0
