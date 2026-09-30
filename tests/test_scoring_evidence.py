"""
Ranking must prefer places that are demonstrably real.

Live failure this pins (Galle, 2026-09-30): a 1-day plan skipped Galle Fort
Ramparts - 4.7 stars, 210 reviews, with a description, the most visited place
in the district - and chose unrated OSM nodes instead. Nothing in the scorer
could tell them apart: with no interests stated `pref` is a flat 0.5 for
everything, `rate` is a flat 0.45 for anything unrated, and `cost` is a flat
0.60 for an unknown price band. That left proximity as the only live factor,
so "nearest" beat "famous".

`evidence` is the factor that separates them. It measures how well-attested a
listing is, not how good it is - an obscure but genuine place still competes on
the other factors.
"""
from app.core.itinerary import TravelMatrix
from app.core.scoring import WEIGHTS, ScoringContext, evidence, rank

ANCHOR = {"lat": 6.0329, "lon": 80.2168}   # Galle Fort

LANDMARK = {
    "id": "landmark", "name": "Galle Fort Ramparts", "lat": 6.0257, "lon": 80.2167,
    "rating": 4.7, "rating_count": 210, "description": "17th-century Dutch fortifications.",
    "opening_hours": "24/7", "tags": [], "price_level": None,
}
# Deliberately CLOSER to the anchor than the landmark: before `evidence`, that
# was enough to win.
ANONYMOUS_NODE = {
    "id": "node", "name": "Bogahagoda", "lat": 6.0330, "lon": 80.2169,
    "rating": None, "rating_count": 0, "description": None,
    "opening_hours": None, "tags": [], "price_level": None,
}


def _rank(items, category="attraction"):
    ctx = ScoringContext(
        interests=[], anchor=ANCHOR, matrix=TravelMatrix(), cost_estimates={}, must_avoid=[],
    )
    return rank(items, ctx, category)


class TestWeightsStayCoherent:
    def test_every_category_sums_to_one(self):
        for category, w in WEIGHTS.items():
            assert round(sum(w.values()), 6) == 1.0, category

    def test_hotels_get_no_evidence_weight(self):
        # Hotels already carry real Booking prices and review scores, so rate
        # and cost separate them. Weighting evidence here also made a
        # better-known mid-priced hotel beat the cheap one under a tight
        # budget, which is a requirement, not a preference.
        assert WEIGHTS["hotel"]["evidence"] == 0.0

    def test_attractions_lean_on_evidence_most(self):
        assert WEIGHTS["attraction"]["evidence"] == max(
            w["evidence"] for w in WEIGHTS.values()
        )


class TestEvidenceScore:
    def test_an_anonymous_node_scores_zero(self):
        assert evidence(ANONYMOUS_NODE) == 0.0

    def test_a_well_attested_landmark_scores_high(self):
        assert evidence(LANDMARK) >= 0.75

    def test_review_count_saturates(self):
        # A 2,000-review place should not bury a solid 200-review one; past
        # saturation the extra reviews say nothing new.
        busy = {**LANDMARK, "rating_count": 2000}
        assert evidence(busy) - evidence(LANDMARK) < 0.05

    def test_a_description_alone_beats_nothing_at_all(self):
        described = {**ANONYMOUS_NODE, "description": "A quiet roadside shrine."}
        assert evidence(described) > evidence(ANONYMOUS_NODE)


class TestRankingOutcome:
    def test_the_landmark_outranks_a_nearer_anonymous_node(self):
        ranked = _rank([ANONYMOUS_NODE, LANDMARK])
        assert ranked[0].item["id"] == "landmark"

    def test_evidence_does_not_override_a_stated_interest(self):
        # Preference still carries the most weight: a traveler who asked for
        # hiking should get the hiking trail, famous or not.
        trail = {**ANONYMOUS_NODE, "id": "trail", "tags": ["hike"]}
        ctx = ScoringContext(
            interests=["hike"], anchor=ANCHOR, matrix=TravelMatrix(),
            cost_estimates={}, must_avoid=[],
        )
        ranked = rank([LANDMARK, trail], ctx, "attraction")
        assert ranked[0].item["id"] == "trail"


class TestPopularityFromPageviews:
    """Wikipedia pageviews, the only popularity signal most attractions have:
    ratings exist on hotels (Booking.com) and essentially nowhere else - 3 of
    339 verified attractions carry one."""

    def _node(self, **over):
        return {**ANONYMOUS_NODE, **over}

    def test_pageviews_alone_lift_an_otherwise_bare_listing(self):
        from app.core.scoring import evidence

        bare = self._node()
        famous = self._node(popularity=57_637)   # Galle Fort, measured 2026-09-30
        assert evidence(bare) == 0.0
        assert evidence(famous) > evidence(bare)

    def test_a_nationally_known_place_outscores_a_minor_one(self):
        from app.core.scoring import evidence

        assert evidence(self._node(popularity=275_216)) > evidence(self._node(popularity=352))

    def test_reviews_and_pageviews_do_not_dilute_each_other(self):
        # Almost nothing carries both, so the better source has to carry the
        # signal - averaging a present one against an absent one would punish a
        # landmark for lacking OSM reviews.
        from app.core.scoring import evidence

        views_only = self._node(popularity=57_637)
        reviews_only = self._node(rating_count=210)
        both = self._node(popularity=57_637, rating_count=210)
        assert evidence(both) == max(evidence(views_only), evidence(reviews_only))

    def test_a_popular_listing_outranks_a_nearer_unknown_one(self):
        popular = {**ANONYMOUS_NODE, "id": "popular", "popularity": 57_637}
        ranked = _rank([ANONYMOUS_NODE, popular])
        assert ranked[0].item["id"] == "popular"


class TestArticleMatching:
    """Matching is where this goes wrong: an earlier coordinate-only pass
    attached the Galle Services Club to three unrelated bastions."""

    def test_containment_is_a_full_match(self):
        from app.data.connectors.wikipedia_popularity import _match_score

        assert _match_score("Galle Fort Ramparts", "Galle Fort") == 1.0

    def test_a_business_named_after_a_landmark_is_rejected(self):
        # "Galle Fort Hotel" is nearer than "Galle Fort" and scored 0.69,
        # which passed the original 0.6 floor and took 1,709 views instead of
        # the landmark's 57,637.
        from app.data.connectors.wikipedia_popularity import _NAME_SIMILARITY_FLOOR, _match_score

        assert _match_score("Galle Fort Ramparts", "Galle Fort Hotel") < _NAME_SIMILARITY_FLOOR

    def test_sharing_only_a_town_name_is_rejected(self):
        from app.data.connectors.wikipedia_popularity import _NAME_SIMILARITY_FLOOR, _match_score

        score = _match_score("Hikkaduwa Coral Reaf", "Hikkaduwa Divisional Secretariat")
        assert score < _NAME_SIMILARITY_FLOOR

    def test_a_genuine_rewording_still_matches(self):
        from app.data.connectors.wikipedia_popularity import _NAME_SIMILARITY_FLOOR, _match_score

        score = _match_score("Temple of the Sacred Tooth Relic", "Temple of the Tooth")
        assert score >= _NAME_SIMILARITY_FLOOR

    def test_an_unrelated_article_is_rejected(self):
        from app.data.connectors.wikipedia_popularity import _NAME_SIMILARITY_FLOOR, _match_score

        assert _match_score("Dutch Entrance", "National Maritime Museum") < _NAME_SIMILARITY_FLOOR

    def test_only_english_wikipedia_tags_are_used(self):
        # Pageviews here are read from en.wikipedia, so a si:/ta: tag is left
        # for the name-matched path rather than queried against the wrong wiki.
        from app.data.connectors.wikipedia_popularity import _title_from_tag

        assert _title_from_tag("en:Galle Fort") == "Galle Fort"
        assert _title_from_tag("si:ගාල්ල කොටුව") is None
