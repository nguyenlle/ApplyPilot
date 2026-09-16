"""Offline source fixtures exercise normalization, extraction, and filtering."""

from unittest.mock import MagicMock, Mock

import pandas as pd
import pytest

from applypilot import database, locfilter
from applypilot.discovery import jobspy, smartextract, workday
from applypilot.enrichment import detail


def test_location_config_uses_shipped_schema_and_empty_defaults():
    cfg = {"location": {"accept_patterns": ["CA"], "reject_patterns": ["London"]}}
    assert locfilter.load_location_filter(cfg) == (["CA"], ["London"])
    assert locfilter.location_ok("San Jose, CA", [], [])
    assert locfilter.location_ok("San Jose, CA", ["CA"], [])
    assert not locfilter.location_ok("Chicago", ["CA"], [])
    assert not locfilter.location_ok("London (Remote)", ["Remote"], ["London"])
    assert locfilter.title_ok("Internal tooling engineer", ["intern"])
    assert not locfilter.title_ok("Mechanical intern", ["intern"])


def test_jobspy_preserves_company_and_avoids_null_url_strings(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "jobs.db")
    monkeypatch.setattr(jobspy.config, "load_search_config", lambda: {"exclude_titles": ["intern"]})
    df = pd.DataFrame([
        {"job_url": "https://example.test/jobs/1", "title": "Mechanical Engineer", "company": "Real Employer",
         "site": "linkedin", "location": "San Jose, CA", "job_url_direct": None, "is_remote": float("nan")},
        {"job_url": "https://example.test/jobs/2", "title": "Mechanical Intern", "company": "Employer",
         "site": "linkedin", "location": "San Jose, CA"},
    ])
    assert jobspy.store_jobspy_results(conn, df, "query")[0] == 1
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["company"] == "Real Employer"
    assert row["application_url"] is None
    assert row["location"] == "San Jose, CA"
    conn.close()


def test_jobspy_persists_freshness_and_structured_salary(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "jobs.db")
    monkeypatch.setattr(jobspy.config, "load_search_config", dict)
    df = pd.DataFrame([{"job_url": "https://example.test/jobs/42", "title": "Mechanical Engineer",
                        "company": "Employer", "site": "linkedin", "date_posted": "2026-09-15",
                        "min_amount": 150000, "max_amount": 180000, "currency": "USD", "interval": "yearly"}])
    assert jobspy.store_jobspy_results(conn, df, "query")[0] == 1
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["date_posted"].startswith("2026-09-15")
    assert row["posted_raw"] == "2026-09-15"
    assert row["salary_min"] == 150000 and row["salary_max"] == 180000
    assert row["salary_currency"] == "USD" and row["salary_interval"] == "yearly"
    conn.close()


def test_workday_keeps_relative_posted_label_without_inventing_date(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "jobs.db")
    monkeypatch.setattr(workday.config, "load_search_config", dict)
    job = {"apply_url": "https://example.test/jobs/42", "title": "Mechanical Engineer",
           "employer_name": "Employer", "posted": "Posted 3 Days Ago", "remote_type": "Hybrid",
           "full_description": "Mechanical design responsibilities and qualifications. " * 20}
    assert workday.store_results(conn, [job], {})[0] == 1
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["date_posted"] is None
    assert row["posted_raw"] == "Posted 3 Days Ago"
    assert row["work_mode"] == "Hybrid"
    conn.close()


def test_flat_search_config_uses_requested_country_sites_and_location(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "jobs.db")
    monkeypatch.setattr(jobspy, "init_db", lambda: conn)
    monkeypatch.setattr(jobspy, "get_connection", lambda: conn)
    scrape = Mock(return_value={"new": 0, "existing": 0, "errors": 0})
    monkeypatch.setattr(jobspy, "_run_one_search", scrape)
    cfg = {"searches": [{"search_term": "mechanical engineer", "location": "Los Angeles, CA", "site_name": ["linkedin"]}],
           "country": "usa", "boards": ["indeed"], "location": {"accept_patterns": ["CA"]}}
    assert jobspy.run_discovery(cfg)["queries"] == 1
    args = scrape.call_args.args
    assert args[0]["location"] == "Los Angeles, CA"
    assert args[1] == ["linkedin"]
    assert args[5]["country_indeed"] == "usa"
    assert args[7] == ["CA"]
    conn.close()


def test_jsonld_type_array_and_relative_apply_url():
    intel = {"json_ld": [{"@graph": [{"@type": ["Thing", "JobPosting"], "description": "Design mechanical fixtures and test their performance. " * 8,
                                     "url": "/apply/42"}]}]}
    extracted = detail.extract_from_json_ld(intel)
    assert "Design mechanical" in extracted["full_description"]
    assert detail._application_url(extracted["application_url"], "https://example.test/jobs/42") == "https://example.test/apply/42"
    assert detail._application_url("javascript:alert(1)", "https://example.test") is None


def test_jsonld_keeps_iso_posted_and_expiry():
    result = detail.extract_from_json_ld({"json_ld": [{"@type": "JobPosting",
        "description": "Mechanical design requirements. " * 20,
        "datePosted": "2026-09-01", "validThrough": "2026-10-01T12:00:00Z"}]})
    assert result["date_posted"].startswith("2026-09-01")
    assert result["valid_through"] == "2026-10-01T12:00:00+00:00"


def test_error_http_page_is_never_promoted_to_description():
    page = Mock()
    page.goto.return_value.status = 429
    result = detail.scrape_detail_page(page, "https://example.test/job")
    assert result["status"] == "error"
    assert result["full_description"] is None
    assert result["error"] == "HTTP 429"
    assert not detail.description_is_usable("Verify you are human. " * 25)


@pytest.mark.parametrize("incoming,status", [(None, "partial"), ("tiny", "partial"),
                                             ("Shorter valid mechanical design description. " * 8, "ok"),
                                             (None, "error")])
def test_enrichment_preserves_existing_long_description(tmp_path, monkeypatch, incoming, status):
    conn = database.init_db(tmp_path / "jobs.db")
    original = "Original complete mechanical design requirements and qualifications. " * 80
    url = "https://example.test/jobs/42"
    app_url = "https://example.test/apply/42"
    conn.execute("INSERT INTO jobs(url,title,full_description,application_url) VALUES (?,?,?,?)",
                 (url, "Mechanical Engineer", original, app_url))
    conn.commit()
    monkeypatch.setattr(detail, "sync_playwright", MagicMock())
    monkeypatch.setattr(detail, "scrape_detail_page", lambda *a: {
        "full_description": incoming, "application_url": None, "status": status,
        "tier_used": 2, "error": "login" if status == "error" else None,
    })
    detail.scrape_site_batch(conn, "fixture", [(url, "Mechanical Engineer")])
    row = conn.execute("SELECT full_description, application_url FROM jobs").fetchone()
    assert row["full_description"] == original
    assert row["application_url"] == app_url
    conn.close()


def test_authwall_redirect_does_not_attempt_llm_extraction(monkeypatch):
    page = Mock()
    page.goto.return_value.status = 200
    page.url = "https://example.test/authwall?original=job"
    page.title.return_value = "Join or sign in"
    llm = Mock(side_effect=AssertionError("Authentication wall must not go to LLM"))
    monkeypatch.setattr(detail, "extract_with_llm", llm)
    assert detail.scrape_detail_page(page, "https://example.test/job")["error"] == "blocked_or_login_page"
    llm.assert_not_called()


@pytest.mark.parametrize("workers", [1, 2])
def test_failing_smart_source_does_not_abort_other_source(tmp_path, monkeypatch, workers):
    conn = database.init_db(tmp_path / "jobs.db")
    monkeypatch.setattr(smartextract, "init_db", lambda: conn)
    monkeypatch.setattr(smartextract.config, "load_search_config", dict)

    def source(name, url):
        if name == "broken":
            raise RuntimeError("timeout")
        return {"name": name, "status": "PASS", "total": 1, "titles": 1,
                "jobs": [{"url": "https://example.test/42", "title": "Mechanical Engineer", "company": "Employer"}]}

    monkeypatch.setattr(smartextract, "_run_one_site", source)
    result = smartextract._run_all([{"name": "broken", "url": "https://example.test/bad"},
                                   {"name": "good", "url": "https://example.test/good"}], [], [], workers=workers)
    assert result["errors"] == 1
    assert result["total_new"] == 1
    conn.close()
