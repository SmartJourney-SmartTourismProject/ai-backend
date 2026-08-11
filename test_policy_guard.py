# test_policy_guard.py
# Run this from your project ROOT (the folder containing app/), e.g.:
#   cd your-project-root
#   python3 test_policy_guard.py

############THIS IS NOT FINALIZE YET BEACUSE THERE CAN BE LOOPHOLES IN THE POLICY GUARD. THIS IS JUST A TESTING FILE FOR NOW.############

from app.utils.policy_guard import check_policy
from app.core.state import TripState

test_cases = [
    # (input, expected_pass)
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
    ("is there an ivory market I can visit", False),
]

passed = 0
for text, expected in test_cases:
    state = TripState(user_input=text)
    result = check_policy(state)
    ok = len(result.errors) == 0
    status = "PASS" if ok == expected else "FAIL"
    if status == "PASS":
        passed += 1
    hit = result.errors[0] if result.errors else None
    print(f"[{status}] expected_pass={expected!s:<5} got_pass={ok!s:<5} hit={hit!r:<60} | {text}")

print(f"\n{passed}/{len(test_cases)} test cases behaved as expected")