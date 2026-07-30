"""Offline ranking, checked against live-measured ground truth.

The ranking exists to decide what gets tried first, not what gets written. Its
only honest test is whether it puts the rows that *actually resolved* above the
rows that did not — so these tests use the outcomes measured live during design
rather than intuitions about which rows look impressive.

That distinction is not academic. An earlier revision penalised "title dilution"
(many people sharing a senior title at one company) on the reasonable-sounding
theory that such titles are meaningless. It demoted **both** verified matches to
the 96th percentile, because both are exactly that shape. The rule was removed.
"""

from __future__ import annotations

import pytest

from linkedin_enrichment.identity import priority


@pytest.fixture
def corpus() -> priority.Corpus:
    """A miniature corpus reproducing the real file's shape."""
    c = priority.Corpus()
    # A very common Indian name pattern. The real file has 23,739 "kumar" and
    # 22,355 "reddy"; the rarity score is log-scaled, so the fixture needs
    # frequencies of a realistic order of magnitude or "Kumar" looks distinctive.
    for i in range(2_500):
        c.observe("Praveen Kumar", f"Shell Ventures {i % 900} Private Limited", "director")
    # Large employers with many same-titled people (the shape that DID resolve).
    for i in range(40):
        c.observe(f"Person{i} Thallapragada{i}", "Wells Fargo International Solutions Private Limited",
                  "executive director")
    for i in range(38):
        c.observe(f"Lawyer{i} Surname{i}", "Dsk Legal Services LLP", "partner")
    # Junk names built from company words.
    for i in range(8):
        c.observe("Rockstar Productions", f"Media House {i} Private Limited", "founder")
        c.observe("Web Hi", f"Web Services {i} Private Limited", "founder")
    # Company-word vocabulary so commercial_word_ratio has something to learn.
    for i in range(30):
        c.observe(f"Real Person{i}", f"Rockstar Productions Web Services {i} Private Limited", "director")
    return c


class TestJunkNames:
    """Name artifacts must never outrank real people, however rare they look."""

    @pytest.mark.parametrize("name", ["Rockstar Productions", "Web Hi"])
    def test_commercial_names_score_zero(self, corpus, name: str) -> None:
        assert corpus.commercial_word_ratio(name) >= 0.5
        assert priority.score_row(corpus, name, "Apple India Private Limited", "founder") == 0

    def test_non_person_scores_zero(self, corpus) -> None:
        assert priority.score_row(
            corpus, "Electronic Manufacturers", "Essae Teraoka Private Limited", "ceo"
        ) == 0

    def test_single_token_name_is_heavily_penalised(self, corpus) -> None:
        single = priority.score_row(corpus, "Rahat", "Onpassive Technologies Private Limited", "founder")
        full = priority.score_row(corpus, "Zaver Jafri", "Onpassive Technologies Private Limited", "founder")
        assert single < full


class TestNameRarity:
    def test_common_token_scores_low(self, corpus) -> None:
        assert corpus.name_rarity("Praveen Kumar") < 0.5

    def test_rare_token_scores_high(self, corpus) -> None:
        assert corpus.name_rarity("Chandrima Thallapragada0") > 0.7

    def test_rarity_uses_the_rarest_token(self, corpus) -> None:
        """One distinctive token is enough to collapse a search."""
        assert corpus.name_rarity("Kumar Thallapragada0") > corpus.name_rarity("Praveen Kumar")


class TestImplausibleRoles:
    def test_founder_of_a_huge_subsidiary_is_penalised(self, corpus) -> None:
        """Nobody founds Apple India. These topped the first ranking pass."""
        assert priority.role_company_implausibility("founder", 78) > 0.5
        assert priority.role_company_implausibility("ceo", 53) > 0.5

    def test_small_company_founder_is_not_penalised(self, corpus) -> None:
        assert priority.role_company_implausibility("founder", 3) == 0.0

    def test_non_founder_roles_are_never_penalised(self, corpus) -> None:
        """Both verified matches hold exactly these titles at huge employers."""
        assert priority.role_company_implausibility("executive director", 299) == 0.0
        assert priority.role_company_implausibility("partner", 38) == 0.0


class TestTitleDilutionStaysDisabled:
    """Regression guard. Re-enabling this demotes both verified matches."""

    def test_dilution_is_a_no_op(self, corpus) -> None:
        assert corpus.role_dilution(
            "Wells Fargo International Solutions Private Limited", "executive director"
        ) == 0.0
        assert corpus.role_dilution("Dsk Legal Services LLP", "partner") == 0.0


class TestGroundTruthOrdering:
    """The measured outcomes: these two resolved live, the junk did not."""

    def test_verified_matches_outrank_junk(self, corpus) -> None:
        verified = priority.score_row(
            corpus, "Chandrima Mitra", "Dsk Legal Services LLP", "partner"
        )
        junk = priority.score_row(
            corpus, "Rockstar Productions", "Apple India Private Limited", "founder"
        )
        assert verified > junk

    def test_verified_match_at_a_huge_employer_still_ranks_well(self, corpus) -> None:
        """Prasanth Thallapragada is 1 of 299 executive directors at Wells Fargo
        and resolved cleanly. Company size must lift, not sink, such rows."""
        score = priority.score_row(
            corpus, "Person1 Thallapragada1",
            "Wells Fargo International Solutions Private Limited", "executive director",
        )
        assert score > 400

    def test_common_name_at_a_shell_ranks_below_a_rare_name_at_a_real_company(
        self, corpus
    ) -> None:
        """Only the ordering matters — that is all the ranking consumes.

        An absolute threshold here would be a statement about the synthetic
        fixture, not about the ranking.
        """
        common = priority.score_row(
            corpus, "Praveen Kumar", "Shell Ventures 1 Private Limited", "director"
        )
        rare = priority.score_row(
            corpus, "Chandrima Mitra", "Dsk Legal Services LLP", "partner"
        )
        assert common < rare


class TestPreferredRoles:
    @pytest.mark.parametrize("role", ["founder", "ceo", "managing director", "partner", "chairman"])
    def test_preferred_roles_recognised(self, role: str) -> None:
        assert priority.is_preferred_role(role)

    @pytest.mark.parametrize("role", ["director", "wholetime director", ""])
    def test_plain_director_is_not_preferred(self, role: str) -> None:
        assert not priority.is_preferred_role(role)

    def test_role_ordering(self) -> None:
        assert priority.role_score("founder") > priority.role_score("managing director")
        assert priority.role_score("managing director") > priority.role_score("partner")
        assert priority.role_score("director") == 0.0
