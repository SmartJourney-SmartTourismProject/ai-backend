"""
Which place each day of a multi-place trip is spent in.

A trip across several places ("3 days in Galle and Matara") is a sequence of
legs, one per place, each with its own hotel. Nights are what a traveller
actually divides between places ("one night by the beach, one somewhere
cheap"), so they are split first and the days follow:

    3 days, Galle + Matara -> 2 nights: Galle, Matara
        day 1  Galle   check in (Galle hotel, 1 night)
        day 2  Matara  check in (Matara hotel, 1 night)
        day 3  Matara  check out

With fewer nights than places (2 days, 2 places) the last place is a day
trip with no hotel of its own. A single place is always one leg - the same
check-in on day 1 / check-out on the last day as before this existed.

Pure: no I/O, so every planner path (the leg builder, fallback, missing-day
fill) derives legs the same way.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DayLeg:
    day: int              # 1-based
    place_index: int      # into the trip's places list
    checkin: bool         # a new hotel is checked into today
    checkout: bool        # the trip's last day, leaving this leg's hotel
    nights: int           # nights stayed in this leg's hotel (0 = day trip)
    leg_start: bool       # first day spent in this place


def _split(total: int, parts: int) -> list[int]:
    """`total` into `parts` near-equal counts, earlier parts taking the extra."""
    base, extra = divmod(total, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def plan_legs(place_count: int, duration_days: int) -> list[DayLeg]:
    days = max(duration_days, 1)
    places = max(min(place_count, days), 1)
    nights = days - 1

    if places == 1:
        return [
            DayLeg(day=d, place_index=0, checkin=(d == 1 and nights > 0), checkout=(d == days and nights > 0),
                   nights=nights, leg_start=(d == 1))
            for d in range(1, days + 1)
        ]

    if nights >= places:
        nights_per_place = _split(nights, places)
        # Night n is spent in place night_place[n-1]; day d is where that
        # day's night is spent, and the last day stays in the final place.
        night_place = [i for i, n in enumerate(nights_per_place) for _ in range(n)]
        day_place = [night_place[d - 1] if d <= nights else night_place[-1] for d in range(1, days + 1)]
    else:
        # Fewer nights than places (only possible when days == places): one
        # place per day, the last one a day trip.
        nights_per_place = [1] * nights + [0] * (places - nights)
        day_place = list(range(places))

    legs = []
    for d in range(1, days + 1):
        place = day_place[d - 1]
        leg_start = d == 1 or day_place[d - 2] != place
        stays = nights_per_place[place] > 0
        legs.append(DayLeg(
            day=d,
            place_index=place,
            checkin=leg_start and stays,
            checkout=(d == days and stays),
            nights=nights_per_place[place],
            leg_start=leg_start,
        ))
    return legs
