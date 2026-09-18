"""Regressions for fact gates, posting identity, retries and document rendering."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from applypilot import artifacts, database, pipeline
from applypilot.scoring import cover_letter, pdf, scorer, tailor, validator


@pytest.fixture
def profile():
    return {
        "personal": {"full_name": "Test Candidate", "email": "candidate@example.test"},
        "skills_boundary": {"engineering": ["CAD", "C++"]},
        "resume_facts": {
            "preserved_companies": ["Example Works"], "preserved_school": "Example University",
            "experience": [{"header": "Engineer at Example Works", "subtitle": "2020-2024"}],
            "education": "Example University | BS Engineering", "real_metrics": ["20%"],
        },
    }


@pytest.fixture
def resume_data():
    return {
        "title": "Mechanical Engineer", "summary": "Designed fixtures using CAD.",
        "skills": {"Engineering": "CAD, C++"},
        "experience": [{"header": "Engineer at Example Works", "subtitle": "2020-2024",
                        "bullets": ["Reduced assembly time by 20% using CAD."]}],
        "projects": [], "education": "Example University | BS Engineering",
    }


def test_profile_skills_are_allowed_and_novel_skills_rejected(profile, resume_data):
    assert validator.validate_json_fields(resume_data, profile)["passed"]
    resume_data["skills"]["Engineering"] += ", Kubernetes"
    assert not validator.validate_json_fields(resume_data, profile, mode="lenient")["passed"]


@pytest.mark.parametrize("mutation", ["title", "dates", "education", "metric"])
def test_immutable_facts_cannot_be_changed(profile, resume_data, mutation):
    if mutation == "title":
        resume_data["experience"][0]["header"] = "Director at Example Works"
    elif mutation == "dates":
        resume_data["experience"][0]["subtitle"] = "2010-2024"
    elif mutation == "education":
        resume_data["education"] += " | PhD"
    else:
        resume_data["experience"][0]["bullets"] = ["Improved throughput by 99%."]
    assert not validator.validate_json_fields(resume_data, profile, original_text="Reduced time by 20%.")["passed"]


@pytest.mark.parametrize("malformed", [[], None, {"title": 42}, {"skills": ["CAD"]}])
def test_malformed_json_is_rejected_without_crashing(profile, malformed):
    assert not validator.validate_json_fields(malformed, profile)["passed"]


@pytest.mark.parametrize("mode", ["strict", "normal", "lenient"])
def test_fact_judge_failure_never_approves(profile, resume_data, monkeypatch, mode):
    monkeypatch.setattr(tailor, "get_client", lambda: Mock(chat=Mock(return_value=json.dumps(resume_data))))
    judge = Mock(return_value={"passed": False, "issues": "Metric attributed to wrong project"})
    monkeypatch.setattr(tailor, "judge_tailored_resume", judge)
    _, report = tailor.tailor_resume("Reduced time by 20%", {"title": "Engineer", "site": "board"},
                                     profile, max_retries=0, validation_mode=mode)
    assert report["status"] == "failed_judge"
    judge.assert_called_once()


def test_failed_cover_letter_is_not_returned(profile, monkeypatch):
    monkeypatch.setattr(cover_letter, "get_client", lambda: Mock(chat=Mock(return_value="Here is an invalid letter")))
    with pytest.raises(ValueError, match="rejected"):
        cover_letter.generate_cover_letter("facts", {"title": "Engineer", "site": "board"}, profile, max_retries=0)


def test_artifacts_are_bound_to_job_and_checksums(tmp_path):
    job = {"url": "https://example.test/job/1", "title": "Engineer", "site": "board"}
    other = {**job, "url": "https://example.test/job/2"}
    assert artifacts.make_filename_prefix(job) != artifacts.make_filename_prefix(other)
    text = tmp_path / f"{artifacts.make_filename_prefix(job)}.txt"
    text.write_text("Validated resume", encoding="utf-8")
    text.with_suffix(".pdf").write_bytes(b"%PDF-1.7 test fixture")
    artifacts.write_manifest(job, text, kind="resume", approved=True)
    assert artifacts.verify_artifact(job, text, kind="resume") == text.with_suffix(".pdf")
    with pytest.raises(ValueError, match="another job"):
        artifacts.verify_artifact(other, text, kind="resume")
    text.write_text("Modified resume", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        artifacts.verify_artifact(job, text, kind="resume")
    with pytest.raises(ValueError, match="changed"):
        artifacts.refresh_pdf_manifest(text)


def test_regeneration_invalidates_previous_pdf(tmp_path):
    text = tmp_path / "resume.txt"
    text.with_suffix(".pdf").write_bytes(b"old")
    text.with_suffix(".manifest.json").write_text("old")
    artifacts.prepare_artifact(text)
    assert not text.with_suffix(".pdf").exists()
    assert not text.with_suffix(".manifest.json").exists()


def test_letter_and_resume_html_preserve_escaped_content():
    letter = pdf._letter_html("Dear Hiring Manager,\n\nBuilt <fixtures> & systems.\n\nCandidate", "Candidate")
    assert "Built &lt;fixtures&gt; &amp; systems." in letter
    assert "Candidate" in letter
    resume = pdf.parse_resume("Candidate\nEngineer\na@example.test\nSUMMARY\nDesigned <fixtures>.\nEXPERIENCE\nACME INC\n2020-2024\n- Built parts.\nEDUCATION\nExample U")
    assert "ACME INC" in resume["sections"]["EXPERIENCE"]
    assert "Designed &lt;fixtures&gt;." in pdf.build_html(resume)


@pytest.mark.parametrize("text,expected", [("**SCORE:** 8", 8), ("SCORE: 7/10", 7), ("SCORE: 11", 0),
                                           ("SCORE: -2", 0), ("SCORE: 8.5", 0), ("error", 0)])
def test_score_parser_rejects_invalid_scores(text, expected):
    assert scorer._parse_score_response(text)["score"] == expected


def test_failed_score_remains_retryable_and_success_commits(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "jobs.db")
    conn.execute("INSERT INTO jobs(url,title,full_description) VALUES (?,?,?)", ("https://example.test/1", "Engineer", "Job facts"))
    conn.commit()
    resume = tmp_path / "resume.txt"
    resume.write_text("Candidate facts", encoding="utf-8")
    monkeypatch.setattr(scorer, "RESUME_PATH", resume)
    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "score_job", lambda *a: {"score": 0, "keywords": "", "reasoning": "timeout"})
    assert scorer.run_scoring()["errors"] == 1
    assert conn.execute("SELECT fit_score FROM jobs").fetchone()[0] is None
    monkeypatch.setattr(scorer, "score_job", lambda *a: {"score": 8, "keywords": "CAD", "reasoning": "match"})
    assert scorer.run_scoring()["scored"] == 1
    assert conn.execute("SELECT fit_score FROM jobs").fetchone()[0] == 8
    conn.close()


def test_streaming_stops_after_repeated_no_progress(monkeypatch):
    tracker = pipeline._StageTracker()
    tracker.mark_done("enrich", {"status": "ok"})
    monkeypatch.setattr(pipeline, "_count_pending", lambda *a: 1)
    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "score", lambda: {"status": "partial", "errors": 1})
    stop = Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False))
    pipeline._run_stage_streaming("score", tracker, stop)
    assert tracker.is_done("score")
    assert tracker.get_results()["score"]["passes"] == 3
    assert "no progress" in tracker.get_results()["score"]["status"]


def test_pipeline_document_chain_two_same_title_postings(tmp_path, monkeypatch, profile, resume_data):
    from applypilot import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    conn = database.init_db(tmp_path / "jobs.db")
    for i in (1, 2):
        conn.execute("INSERT INTO jobs(url,title,site,company,full_description,fit_score) VALUES (?,?,?,?,?,?)",
                     (f"https://example.test/{i}", "Engineer", "board", f"Employer {i}", "Job description", 8))
    conn.commit()
    resume = tmp_path / "base.txt"
    resume.write_text("Reduced assembly time by 20%", encoding="utf-8")
    for module in (tailor, cover_letter):
        monkeypatch.setattr(module, "get_connection", lambda: conn)
        monkeypatch.setattr(module, "RESUME_PATH", resume)
        monkeypatch.setattr(module, "load_profile", lambda: profile)
    monkeypatch.setattr(tailor, "TAILORED_DIR", tmp_path / "tailored")
    monkeypatch.setattr(cover_letter, "COVER_LETTER_DIR", tmp_path / "letters")
    monkeypatch.setattr(tailor, "get_client", lambda: Mock(chat=Mock(return_value=json.dumps(resume_data))))
    monkeypatch.setattr(tailor, "judge_tailored_resume", lambda *a: {"passed": True, "issues": "none"})
    monkeypatch.setattr(cover_letter, "get_client", lambda: Mock(chat=Mock(return_value="Dear Hiring Manager,\n\nDesigned fixtures using CAD.\n\nTest Candidate")))
    monkeypatch.setattr(cover_letter, "judge_tailored_resume", lambda *a: {"passed": True, "issues": "none"})
    monkeypatch.setattr(pdf, "render_pdf", lambda html, output: Path(output).write_bytes(b"%PDF-1.7\n" + html.encode()))
    assert tailor.run_tailoring(limit=0)["approved"] == 2
    assert cover_letter.run_cover_letters(limit=0)["generated"] == 2
    jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs")]
    assert len({j["tailored_resume_path"] for j in jobs}) == 2
    for job in jobs:
        assert len(artifacts.verify_job_artifacts(job)) == 2
    wrong_job = {**jobs[0], "tailored_resume_path": jobs[1]["tailored_resume_path"]}
    with pytest.raises(ValueError):
        artifacts.verify_job_artifacts(wrong_job)
    conn.close()
