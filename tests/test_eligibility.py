"""Hard preference gates use explicit evidence and preserve unknowns for review."""

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from applypilot.eligibility import eligibility_reason
from applypilot.scoring import scorer


@pytest.fixture
def cfg():
    return {"preferences": {
        "target_roles": ["mechanical engineer", "product design engineer", "mechanical design engineer"],
        "locations": ["San Jose", "Los Angeles"], "work_modes": ["remote", "hybrid", "onsite"],
        "salary_min": 150000, "salary_currency": "USD", "requires_sponsorship": True,
    }}


@pytest.fixture
def job():
    return {"title": "Senior Mechanical Design Engineer", "company": "Example Employer",
            "location": "San Jose, CA (Hybrid)", "url": "https://jobs.example.test/42",
            "salary": "USD140,000-USD180,000/year", "full_description": "Design mechanical fixtures."}


def test_qualified_salary_range_and_role_are_allowed(cfg, job):
    assert eligibility_reason(job, cfg, phase="apply") is None


@pytest.mark.parametrize("field,value", [("title", "UX Product Design Engineer"), ("title", "Product Designer"),
                                         ("location", "Austin, TX"), ("salary", "USD100,000-USD140,000/year")])
def test_explicit_preference_mismatch_is_rejected(cfg, job, field, value):
    job[field] = value
    assert eligibility_reason(job, cfg).startswith("not_eligible:")


@pytest.mark.parametrize("field", ["salary", "location", "title"])
def test_unknown_required_apply_information_is_not_assumed(cfg, job, field):
    job[field] = None
    assert eligibility_reason(job, cfg, phase="apply").startswith("missing_info:")
    assert eligibility_reason(job, cfg, phase="score") is None


def test_unknown_salary_can_be_explicitly_permitted(cfg, job):
    job["salary"] = None
    cfg["preferences"]["require_salary_for_apply"] = False
    assert eligibility_reason(job, cfg, phase="apply") is None


@pytest.mark.parametrize("salary", ["USD80-USD100/hour", "CAD180,000/year", "USD150,000+ per year",
                                    "USD140,000 starting annual salary", "USD140,000-USD180,000"])
def test_incomparable_salary_is_not_treated_as_known_annual_maximum(cfg, job, salary):
    job["salary"] = salary
    assert eligibility_reason(job, cfg, phase="score") is None
    assert eligibility_reason(job, cfg, phase="apply").startswith("missing_info:")


def test_structured_annual_salary_below_minimum(cfg, job):
    job.update(salary=None, salary_max=149999, salary_currency="USD", salary_interval="yearly")
    assert "salary maximum" in eligibility_reason(job, cfg)


@pytest.mark.parametrize("description", ["No visa sponsorship available.", "We cannot provide sponsorship.",
                                         "Candidates must work without future sponsorship.", "Sponsorship is not offered."])
def test_explicit_sponsorship_denial_wins_over_missing_salary(cfg, job, description):
    job.update(full_description=description, salary=None)
    assert eligibility_reason(job, cfg, phase="apply").startswith("not_eligible:")


def test_unrelated_denial_is_not_sponsorship_denial(cfg, job):
    job["full_description"] = "We cannot provide relocation benefits. Visa sponsorship is available."
    assert eligibility_reason(job, cfg, phase="apply") is None


def test_profile_string_yes_requires_sponsorship(cfg, job):
    cfg["preferences"].pop("requires_sponsorship")
    job["full_description"] = "We cannot provide visa sponsorship."
    profile = {"work_authorization": {"require_sponsorship": "Yes"}}
    assert "sponsorship" in eligibility_reason(job, cfg, profile)


@pytest.mark.parametrize("key,value", [("exclude_companies", ["Example Employer"]), ("exclude_titles", ["senior"]),
                                       ("exclude_locations", ["San Jose"]), ("exclude_domains", ["example.test"])])
def test_configured_exclusions(cfg, job, key, value):
    cfg[key] = value
    assert eligibility_reason(job, cfg).startswith("not_eligible:")


def test_domain_exclusion_does_not_match_lookalike(cfg, job):
    cfg["exclude_domains"] = ["example.test"]
    job["url"] = "https://notexample.test/job"
    assert eligibility_reason(job, cfg) is None


def test_work_mode_restrictions_and_not_remote(cfg, job):
    cfg["preferences"]["work_modes"] = ["remote"]
    job["work_mode"] = "not remote"
    assert "work mode" in eligibility_reason(job, cfg)


def test_remote_geographic_unknown_is_not_assumed(cfg, job):
    job["location"] = "Remote"
    assert eligibility_reason(job, cfg, phase="score") is None
    assert eligibility_reason(job, cfg, phase="apply").startswith("missing_info:")


def test_stale_posting_and_expiration(cfg, job):
    cfg["preferences"]["max_job_age_days"] = 30
    job["date_posted"] = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    assert "older" in eligibility_reason(job, cfg)
    job.pop("date_posted")
    job["valid_through"] = "2020-01-01T00:00:00Z"
    assert "expired" in eligibility_reason(job, cfg)


def test_negative_gate_prevents_paid_llm_call(monkeypatch, job):
    monkeypatch.setattr(scorer, "eligibility_reason", lambda *a, **kw: "not_eligible: excluded company")
    client = Mock(side_effect=AssertionError("LLM must not be called"))
    monkeypatch.setattr(scorer, "get_client", client)
    assert scorer.score_job("Candidate facts", job)["score"] == 1
    client.assert_not_called()

@pytest.mark.parametrize("description", [
    "This Role Is NOT For You If\nYou prefer desk work.\nYou require visa sponsorship to work here.\nNext steps\nApply.",
    "**This Role Is NOT For You If** \n\n\n* You prefer desk work.\n* You require visa sponsorship to work here.\n\n**Next steps**",
])
def test_negative_eligibility_section_denies_sponsorship(cfg, job, description):
    job["full_description"] = description
    assert "sponsorship" in eligibility_reason(job, cfg)


def test_negative_section_stops_before_later_sponsorship_guidance(cfg, job):
    job["full_description"] = (
        "This Role Is NOT For You If\nYou prefer desk work.\nBenefits\n"
        "You require visa sponsorship? We can support you."
    )
    assert eligibility_reason(job, cfg) is None
