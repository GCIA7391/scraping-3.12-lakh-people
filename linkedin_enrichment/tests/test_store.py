"""State store: claiming, checkpointing and the resume guarantee."""

from __future__ import annotations

import time

import pytest

from linkedin_enrichment.database.store import Store


def seed(store: Store, count: int = 10) -> None:
    store.insert_records([
        {
            "row_uid": f"uid{i:03d}", "source_file": "f.csv", "row_index": i,
            "name": f"Person {i}", "company": f"Company {i} Private Limited",
            "company_key": f"company-{i}", "dedup_key": f"person-{i}|company-{i}",
        }
        for i in range(count)
    ])


class TestIngestion:
    def test_insert_is_idempotent(self, store: Store) -> None:
        """Re-ingesting the same file must not duplicate or error — this is what
        makes `run` behave as `resume` on an existing database."""
        seed(store, 5)
        assert store.scalar("SELECT COUNT(*) FROM records") == 5
        seed(store, 5)
        assert store.scalar("SELECT COUNT(*) FROM records") == 5


class TestClaiming:
    def test_claim_removes_rows_from_the_pending_pool(self, store: Store) -> None:
        seed(store, 10)
        first = store.claim_batch(4)
        second = store.claim_batch(4)
        assert len(first) == 4 and len(second) == 4
        assert {r["row_uid"] for r in first}.isdisjoint({r["row_uid"] for r in second})

    def test_claim_returns_empty_when_drained(self, store: Store) -> None:
        seed(store, 3)
        store.claim_batch(10)
        assert store.claim_batch(10) == []

    def test_release_returns_rows_to_pending(self, store: Store) -> None:
        seed(store, 5)
        claimed = store.claim_batch(5)
        store.release([r["row_uid"] for r in claimed])
        assert store.scalar("SELECT COUNT(*) FROM records WHERE status='pending'") == 5


class TestResume:
    def test_stale_claims_are_reclaimed(self, store: Store) -> None:
        """A worker that died mid-row must not strand its work forever."""
        seed(store, 5)
        store.claim_batch(5)
        # Simulate claims made well in the past.
        store.force_claimed_at(time.time() - 10_000)

        assert store.reclaim_stale(900) == 5
        assert store.scalar("SELECT COUNT(*) FROM records WHERE status='pending'") == 5

    def test_fresh_claims_are_not_reclaimed(self, store: Store) -> None:
        seed(store, 5)
        store.claim_batch(5)
        assert store.reclaim_stale(900) == 0

    def test_completed_work_is_never_redone(self, store: Store) -> None:
        seed(store, 5)
        for record in store.claim_batch(3):
            store.write_result(record["row_uid"], decision="matched",
                               linkedin_url="https://www.linkedin.com/in/x", confidence=0.99)
        store.reclaim_stale(0)  # aggressively reclaim everything reclaimable
        remaining = store.claim_batch(100)
        assert len(remaining) == 2, "done rows must not be re-claimed"


class TestResults:
    def test_write_result_marks_row_done_atomically(self, store: Store) -> None:
        seed(store, 1)
        store.claim_batch(1)
        store.write_result("uid000", decision="matched",
                           linkedin_url="https://www.linkedin.com/in/x", confidence=0.97)
        assert store.scalar("SELECT status FROM records WHERE row_uid='uid000'") == "done"
        assert store.get_result("uid000")["confidence"] == pytest.approx(0.97)

    def test_result_write_is_upsert(self, store: Store) -> None:
        seed(store, 1)
        store.write_result("uid000", decision="blank_low_confidence", confidence=0.4)
        store.write_result("uid000", decision="matched", confidence=0.99,
                           linkedin_url="https://www.linkedin.com/in/x")
        assert store.scalar("SELECT COUNT(*) FROM results") == 1
        assert store.get_result("uid000")["decision"] == "matched"


class TestSharedUrlRetraction:
    def test_a_profile_claimed_by_two_people_is_retracted(self, store: Store) -> None:
        """One profile cannot be two people, so neither claim survives."""
        seed(store, 3)
        shared = "https://www.linkedin.com/in/praveen-kumar"
        store.write_result("uid000", decision="matched", linkedin_url=shared, confidence=0.98)
        store.write_result("uid001", decision="matched", linkedin_url=shared, confidence=0.97)
        store.write_result("uid002", decision="matched", confidence=0.99,
                           linkedin_url="https://www.linkedin.com/in/unique-person")

        assert store.retract_shared_urls() == 2
        assert store.get_result("uid000")["decision"] == "blank_ambiguous"
        assert store.get_result("uid000")["linkedin_url"] == ""
        assert store.get_result("uid001")["linkedin_url"] == ""
        # The uncontested match is untouched.
        assert store.get_result("uid002")["decision"] == "matched"

    def test_the_same_person_on_two_rows_keeps_their_match(self, store: Store) -> None:
        """Regression: 3,660 rows in the production file are exact duplicates of
        another row. Those rows legitimately share one profile URL, and counting
        rows instead of distinct people would retract every one of them."""
        store.insert_records([
            {
                "row_uid": "dup-a", "source_file": "f.csv", "row_index": 1,
                "name": "Girish Rowjee", "company": "Greytip Software Private Limited",
                "company_key": "greytip-software",
                "dedup_key": "girish-rowjee|greytip-software",
            },
            {
                "row_uid": "dup-b", "source_file": "f.csv", "row_index": 2,
                "name": "Girish Rowjee", "company": "Greytip Software Private Limited",
                "company_key": "greytip-software",
                "dedup_key": "girish-rowjee|greytip-software",
            },
        ])
        url = "https://www.linkedin.com/in/girishrowjee"
        store.write_result("dup-a", decision="matched", linkedin_url=url, confidence=0.99)
        store.write_result("dup-b", decision="matched", linkedin_url=url, confidence=0.99)

        assert store.retract_shared_urls() == 0
        assert store.get_result("dup-a")["linkedin_url"] == url
        assert store.get_result("dup-b")["linkedin_url"] == url


class TestSuppression:
    def test_suppress_erases_and_tombstones(self, store: Store) -> None:
        seed(store, 2)
        store.write_result("uid000", decision="matched", confidence=0.99,
                           linkedin_url="https://www.linkedin.com/in/someone")
        store.add_review_candidates("uid000", [
            {"linkedin_url": "https://www.linkedin.com/in/other", "confidence": 0.7}
        ])

        store.suppress("uid000", note="deletion request")

        assert store.is_suppressed("uid000")
        assert store.get_result("uid000")["linkedin_url"] == ""
        assert store.get_result("uid000")["decision"] == "suppressed"
        assert store.scalar(
            "SELECT COUNT(*) FROM review_candidates WHERE row_uid='uid000'"
        ) == 0
        # A suppressed row must not be handed back out for processing.
        assert all(r["row_uid"] != "uid000" for r in store.claim_batch(10))


class TestCaches:
    def test_company_negative_cache_round_trip(self, store: Store) -> None:
        store.put_company("natesh-impex", brand_name="natesh impex", has_footprint=False)
        row = store.get_company("natesh-impex")
        assert row["has_linkedin_footprint"] == 0

    def test_serp_cache_round_trip_and_ttl(self, store: Store) -> None:
        store.put_serp("h1", "some query", "cassette", {"results": [{"title": "t"}]})
        assert store.get_serp("h1") is not None
        # A zero-length TTL expires everything.
        assert store.get_serp("h1", ttl_seconds=-1) is None
