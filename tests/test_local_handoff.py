"""Adverse local handoff tests: synthetic inputs, no model or employer writes."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from applypilot import config, database
from applypilot import local_handoff as handoff
from applypilot.apply import state
from applypilot.policy import RuntimePolicy


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def pdf(path):
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(path)


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def units(resume, paragraphs):
    values = [resume["title"], resume["summary"], resume["education"], *paragraphs]
    for skills in resume["skills"].values():
        values.extend(skills)
    for entry in resume["experience"] + resume["projects"]:
        values.extend([entry["header"], entry["subtitle"], *entry["bullets"]])
    return sorted(set(filter(None, values)))


@pytest.fixture
def local(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setattr(config, "APP_DIR", private)
    for attr, name in (("PROFILE_PATH", "profile.json"), ("RESUME_PATH", "resume.txt"),
                       ("RESUME_PDF_PATH", "resume.pdf"), ("SEARCH_CONFIG_PATH", "searches.yaml"),
                       ("DB_PATH", "jobs.db")):
        monkeypatch.setattr(config, attr, private / name)
    monkeypatch.setattr(database, "DB_PATH", config.DB_PATH)
    monkeypatch.setattr(config, "load_blocked_sites", lambda: (set(), []))
    monkeypatch.setattr(config, "is_manual_ats", lambda _: False)
    monkeypatch.setattr(state, "load_policy", RuntimePolicy)
    resume = {
        "title": "Mechanical Engineer", "summary": "Designed fixtures using CAD.",
        "skills": {"Engineering": ["CAD"]},
        "experience": [{"header": "Engineer at Example Works", "subtitle": "2020-2024",
                        "bullets": ["Reduced assembly time by 20% using CAD."]}],
        "projects": [], "education": "Example University | BS Engineering",
    }
    profile = {
        "personal": {"full_name": "Synthetic Candidate", "email": "candidate@example.test"},
        "skills_boundary": {"engineering": ["CAD"]},
        "resume_facts": {"preserved_companies": ["Example Works"],
                         "preserved_school": "Example University",
                         "experience": [{"header": "Engineer at Example Works", "subtitle": "2020-2024"}],
                         "education": resume["education"], "real_metrics": ["20%"]},
        "credentials": {"password": "DO_NOT_EXPORT_SYNTHETIC_SECRET"},
        "eeo": {"gender": "DO_NOT_EXPORT_SYNTHETIC_EEO"},
    }
    write_json(config.PROFILE_PATH, profile)
    config.RESUME_PATH.write_text("\n".join(units(resume, [])), encoding="utf-8")
    pdf(config.RESUME_PDF_PATH)
    config.SEARCH_CONFIG_PATH.write_text("{}", encoding="utf-8")
    conn = database.init_db(config.DB_PATH)
    url = "https://jobs.example.test/view?requisitionId=123"
    conn.execute("INSERT INTO jobs (url,title,company,site,location,full_description,apply_attempts) "
                 "VALUES (?,?,?,?,?,?,?)", (url, "Mechanical Engineer", "Synthetic Employer", "synthetic",
                                           "Synthetic City", "Design fixtures using CAD. Mechanical Engineer", 2))
    conn.commit()
    yield SimpleNamespace(private=private, conn=conn, url=url, resume=resume, profile=profile)
    database.close_connection(config.DB_PATH)


def job(local):
    return dict(local.conn.execute("SELECT * FROM jobs WHERE url=?", (local.url,)).fetchone())


def bundle(local, score=8):
    path = handoff.export_job(local.url)
    folder = path.parent
    paragraphs = [local.resume["summary"]]
    result = {
        "version": 2, "author": "synthetic-author", "handoff_sha256": handoff.digest(path),
        "job_url": local.url, "source": handoff.SOURCE,
        "score": {"value": score, "reasoning": "Fixture design aligns.",
                  "evidence": "CAD and fixture work documented.", "gaps": "Other skills remain unknown."},
        "resume": deepcopy(local.resume), "cover_paragraphs": paragraphs,
        "claims": [{"text": text, "kind": "applicant", "evidence": [{"source": "resume", "quote": text}]}
                   for text in units(local.resume, paragraphs)],
    }
    result_path = folder / "result.json"
    write_json(result_path, result)
    _, _, _, resume_text, cover_text = handoff.validate_result(path, result_path)
    for name, text in (("resume-tailored", resume_text), ("cover-letter", cover_text)):
        (folder / f"{name}.txt").write_text(text, encoding="utf-8")
        pdf(folder / f"{name}.pdf")
    review = {
        "version": 2, "source": handoff.SOURCE, "reviewer": "synthetic-independent-reviewer",
        "result_sha256": handoff.digest(result_path), "handoff_sha256": handoff.digest(path),
        "semantic_claims_checked": True, "all_pdf_pages_inspected": True,
        "notes": "Synthetic review fixture; blank PDF is a unit test, not a usable resume.",
        "files": {name: handoff.digest(folder / name) for name in (
            "resume-tailored.txt", "resume-tailored.pdf", "cover-letter.txt", "cover-letter.pdf")},
        "claims": [{"text_sha256": text_hash(c["text"]), "verdict": "supported", "notes": "Exact synthetic source."}
                   for c in result["claims"]],
        "pdf_pages": {"resume-tailored.pdf": [1], "cover-letter.pdf": [1]},
    }
    review_path = folder / "review.json"
    write_json(review_path, review)
    return SimpleNamespace(path=path, folder=folder, result=result, result_path=result_path,
                           review=review, review_path=review_path)


def import_bundle(b):
    return handoff.import_result(b.path, b.result_path, b.review_path)


def assert_rejected_clean(local, b):
    before = job(local)
    with pytest.raises(ValueError):
        import_bundle(b)
    assert job(local) == before
    assert not list(b.folder.glob("*.manifest.json"))


def assert_result_rejected(local, b):
    # A stale review hash must not hide a missing content check.
    with pytest.raises(ValueError):
        handoff.validate_result(b.path, b.result_path)
    assert_rejected_clean(local, b)


@pytest.mark.parametrize("url", ["https://jobs.example.test/view?requisitionId=12", "%", "_", "unknown"])
def test_export_requires_exact_existing_url(local, url):
    with pytest.raises(ValueError):
        handoff.export_job(url)


def test_export_projects_profile_without_secrets_or_unrelated_eeo(local):
    b = bundle(local)
    snapshot = (b.folder / "profile.json").read_text(encoding="utf-8")
    assert "DO_NOT_EXPORT" not in snapshot
    assert "Synthetic Candidate" in snapshot
    assert job(local)["fit_score"] is None
    assert not list(b.folder.glob("*.manifest.json"))


@pytest.mark.parametrize("name", ["profile.json", "resume.txt", "resume.pdf", "searches.yaml"])
@pytest.mark.parametrize("target", ["source", "snapshot"])
def test_changed_source_or_snapshot_rejects_import(local, name, target):
    b = bundle(local)
    path = (local.private if target == "source" else b.folder) / name
    path.write_bytes(path.read_bytes() + b"\n ")
    assert_rejected_clean(local, b)


@pytest.mark.parametrize("field,value", [("full_description", "Changed job"), ("company", "Other employer"),
    ("apply_status", "uncertain"), ("claim_token", "live-claim"), ("applied_at", "2026-09-16T00:00:00Z")])
def test_changed_job_cannot_import_over_current_application_state(local, field, value):
    b = bundle(local)
    local.conn.execute(f"UPDATE jobs SET {field}=? WHERE url=?", (value, local.url))
    local.conn.commit()
    assert_rejected_clean(local, b)


@pytest.mark.parametrize("field,value", [
    ("apply_status", "applied"), ("submitted", True), ("version", True), ("version", 1),
    ("job_url", "https://jobs.example.test/view?requisitionId=1234"), ("source", "remote_api"),
    ("handoff_sha256", "0" * 64), ("author", ""), ("score", []), ("resume", []),
    ("claims", [None]), ("cover_paragraphs", "not a list"),
])
def test_malformed_or_unbound_result_rejected(local, field, value):
    b = bundle(local)
    b.result[field] = value
    write_json(b.result_path, b.result)
    assert_result_rejected(local, b)


@pytest.mark.parametrize("field,value", [("value", True), ("value", 11), ("value", 0),
    ("value", "8"), ("force_queue", True), ("evidence", []), ("gaps", "")])
def test_score_schema_and_limits_are_strict(local, field, value):
    b = bundle(local)
    b.result["score"][field] = value
    write_json(b.result_path, b.result)
    assert_result_rejected(local, b)


@pytest.mark.parametrize("mutation", ["education", "date", "metric", "unknown_skill", "extra_field", "skill_string"])
def test_immutable_facts_and_numeric_claims_are_checked(local, mutation):
    b = bundle(local)
    data = b.result["resume"]
    if mutation == "education":
        data["education"] = "Example University | PhD Engineering"
    elif mutation == "date":
        data["experience"][0]["subtitle"] = "2010-2024"
    elif mutation == "metric":
        data["experience"][0]["bullets"] = ["Reduced assembly time by 99% using CAD."]
    elif mutation == "unknown_skill":
        data["skills"]["Engineering"] = ["SolidWorks"]
    elif mutation == "extra_field":
        data["approved"] = True
    else:
        data["skills"]["Engineering"] = "CAD"
    write_json(b.result_path, b.result)
    assert_result_rejected(local, b)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "fabricated_quote", "job_only", "extra_field"])
def test_claim_evidence_must_cover_candidate_facts(local, mutation):
    b = bundle(local)
    if mutation == "missing":
        b.result["claims"].pop()
    elif mutation == "duplicate":
        b.result["claims"].append(deepcopy(b.result["claims"][0]))
    elif mutation == "fabricated_quote":
        b.result["claims"][0]["evidence"][0]["quote"] = "Unsupported source quote"
    elif mutation == "job_only":
        b.result["claims"][0]["evidence"] = [{"source": "job", "quote": "Design fixtures using CAD."}]
    else:
        b.result["claims"][0]["approved"] = True
    write_json(b.result_path, b.result)
    assert_result_rejected(local, b)


@pytest.mark.parametrize("field,value", [
    ("version", True), ("reviewer", "synthetic-author"), ("reviewer", ""),
    ("semantic_claims_checked", "true"), ("all_pdf_pages_inspected", False),
    ("notes", []), ("result_sha256", "0" * 64), ("handoff_sha256", "0" * 64),
    ("approved", True), ("claims", []), ("pdf_pages", {}), ("files", []),
])
def test_independent_review_schema_and_bindings(local, field, value):
    b = bundle(local)
    b.review[field] = value
    write_json(b.review_path, b.review)
    assert_rejected_clean(local, b)


@pytest.mark.parametrize("mutation", ["bad_claim_hash", "duplicate_claim", "unsupported", "missing_page", "extra_page", "extra_file"])
def test_review_covers_exact_claims_files_and_actual_pdf_pages(local, mutation):
    b = bundle(local)
    if mutation == "bad_claim_hash":
        b.review["claims"][0]["text_sha256"] = "0" * 64
    elif mutation == "duplicate_claim":
        b.review["claims"].append(deepcopy(b.review["claims"][0]))
    elif mutation == "unsupported":
        b.review["claims"][0]["verdict"] = "unsupported"
    elif mutation == "missing_page":
        b.review["pdf_pages"]["resume-tailored.pdf"] = []
    elif mutation == "extra_page":
        b.review["pdf_pages"]["resume-tailored.pdf"] = [1, 2]
    else:
        b.review["files"]["another-file.txt"] = "0" * 64
    write_json(b.review_path, b.review)
    assert_rejected_clean(local, b)


@pytest.mark.parametrize("name", ["resume-tailored.txt", "resume-tailored.pdf", "cover-letter.txt", "cover-letter.pdf"])
def test_changed_artifact_after_review_cannot_gain_manifest(local, name):
    b = bundle(local)
    path = b.folder / name
    path.write_bytes(path.read_bytes() + b"\nchanged")
    assert_rejected_clean(local, b)


def test_import_updates_preparation_only_and_replay_is_rejected(local):
    local.conn.execute("UPDATE jobs SET apply_status='queued',apply_error='prior diagnostic' WHERE url=?", (local.url,))
    local.conn.commit()
    b = bundle(local)
    before = job(local)
    result = import_bundle(b)
    after = job(local)
    assert result["submitted"] is False
    assert result["application_status_changed"] is False
    allowed = {"fit_score", "score_reasoning", "scored_at", "tailored_resume_path", "tailored_at", "cover_letter_path", "cover_letter_at"}
    assert {k for k in before if before[k] != after[k]} <= allowed
    assert after["fit_score"] == 8
    assert json.loads(after["score_reasoning"])["source"] == handoff.SOURCE
    manifests = {p: p.read_bytes() for p in b.folder.glob("*.manifest.json")}
    assert len(manifests) == 2
    with pytest.raises(ValueError):
        import_bundle(b)
    assert job(local) == after
    assert all(p.read_bytes() == content for p, content in manifests.items())


def test_score_six_import_does_not_make_job_apply_eligible(local):
    b = bundle(local, score=6)
    import_bundle(b)
    assert job(local)["fit_score"] == 6
    assert state.select_jobs(dry_run=True) is None
    assert job(local)["apply_status"] is None


def test_database_failure_rolls_back_approvals_and_preparation(local):
    b = bundle(local)
    local.conn.execute("CREATE TRIGGER deny_preparation BEFORE UPDATE ON jobs BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    local.conn.commit()
    before = job(local)
    with pytest.raises((sqlite3.DatabaseError, ValueError)):
        import_bundle(b)
    assert job(local) == before
    assert not list(b.folder.glob("*.manifest.json"))
    local.conn.execute("DROP TRIGGER deny_preparation")
    local.conn.commit()
    assert import_bundle(b)["preparation_imported"] is True


def test_simultaneous_imports_have_one_winner(local):
    b = bundle(local)

    def attempt():
        try:
            return import_bundle(b)["preparation_imported"]
        except ValueError:
            return False
        finally:
            database.close_connection(config.DB_PATH)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]
    assert job(local)["apply_attempts"] == 2
    assert job(local)["applied_at"] is None


@pytest.mark.parametrize("name", ["result.json", "review.json"])
def test_result_and_review_must_use_pinned_names_and_folder(local, name):
    b = bundle(local)
    old = b.folder / name
    renamed = b.folder / ("renamed-" + name)
    old.rename(renamed)
    if name == "result.json":
        b.result_path = renamed
    else:
        b.review_path = renamed
    assert_rejected_clean(local, b)


@pytest.mark.parametrize("target", ["source", "snapshot", "artifact"])
def test_symlink_even_with_same_bytes_is_rejected(local, target):
    b = bundle(local)
    path = config.RESUME_PATH if target == "source" else b.folder / ("resume.txt" if target == "snapshot" else "resume-tailored.pdf")
    outside = local.private.parent / "outside.txt"
    outside.write_bytes(path.read_bytes())
    original = path.read_bytes()
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError as exc:
        path.write_bytes(original)
        pytest.skip(f"Host does not permit test symlinks: {exc}")
    assert_rejected_clean(local, b)
    assert outside.read_bytes() == original


def test_forged_handoff_cannot_rebind_changed_job(local):
    b = bundle(local)
    local.conn.execute("UPDATE jobs SET company='Forged employer' WHERE url=?", (local.url,))
    local.conn.commit()
    document = handoff.read(b.path)
    document["job"] = job(local)
    document["job_sha256"] = handoff.object_digest(document["job"])
    write_json(b.path, document)
    b.result["handoff_sha256"] = handoff.digest(b.path)
    write_json(b.result_path, b.result)
    b.review["handoff_sha256"] = handoff.digest(b.path)
    b.review["result_sha256"] = handoff.digest(b.result_path)
    write_json(b.review_path, b.review)
    assert_rejected_clean(local, b)


@pytest.mark.parametrize("filename", ["result.json", "review.json"])
def test_duplicate_json_fields_are_rejected(local, filename):
    b = bundle(local)
    path = b.folder / filename
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('{', '{"version": 2,', 1), encoding="utf-8")
    assert_rejected_clean(local, b)


@pytest.mark.parametrize("value", [[True], [0], [1, 1], ["1"]])
def test_pdf_review_page_numbers_are_strict(local, value):
    b = bundle(local)
    b.review["pdf_pages"]["resume-tailored.pdf"] = value
    write_json(b.review_path, b.review)
    assert_rejected_clean(local, b)


@pytest.mark.parametrize("target", ["result", "review"])
def test_outside_private_paths_are_rejected(local, target):
    b = bundle(local)
    old = b.result_path if target == "result" else b.review_path
    outside = local.private.parent / old.name
    outside.write_bytes(old.read_bytes())
    if target == "result":
        b.result_path = outside
    else:
        b.review_path = outside
    assert_rejected_clean(local, b)


def test_job_context_cannot_be_used_as_resume_evidence(local):
    b = bundle(local)
    claim = next(c for c in b.result["claims"] if c["text"] == "Mechanical Engineer")
    claim["kind"] = "job_context"
    claim["evidence"] = [{"source": "job", "quote": "Mechanical Engineer"}]
    write_json(b.result_path, b.result)
    assert_result_rejected(local, b)


def test_job_context_cannot_cover_duplicate_resume_prose(local):
    b = bundle(local)
    b.result["cover_paragraphs"].append("Mechanical Engineer")
    claim = next(c for c in b.result["claims"] if c["text"] == "Mechanical Engineer")
    claim["kind"] = "job_context"
    claim["evidence"] = [{"source": "job", "quote": "Mechanical Engineer"}]
    write_json(b.result_path, b.result)
    assert_result_rejected(local, b)


def test_hardlinked_artifact_is_rejected_even_with_same_review_hash(local):
    b = bundle(local)
    artifact = b.folder / "resume-tailored.pdf"
    link = local.private.parent / "external-copy.pdf"
    link.hardlink_to(artifact)
    assert_rejected_clean(local, b)


def test_render_changes_neither_database_nor_approvals(local, monkeypatch):
    from applypilot.scoring import pdf as renderers

    b = bundle(local)
    before = job(local)
    monkeypatch.setattr(renderers, "convert_to_pdf", lambda path: pdf(path.with_suffix(".pdf")))
    monkeypatch.setattr(renderers, "convert_letter_to_pdf", lambda path: pdf(path.with_suffix(".pdf")))
    assert handoff.render_result(b.path, b.result_path) == b.folder
    assert job(local) == before
    assert not list(b.folder.glob("*.manifest.json"))


def test_second_manifest_failure_removes_first_manifest(local, monkeypatch):
    b = bundle(local)
    original = handoff.write_manifest

    def fail_cover(job, path, *, kind, approved, **kwargs):
        if kind == "cover_letter":
            raise ValueError("synthetic filesystem failure")
        return original(job, path, kind=kind, approved=approved, **kwargs)

    monkeypatch.setattr(handoff, "write_manifest", fail_cover)
    assert_rejected_clean(local, b)


def test_changed_inputs_during_manifest_creation_roll_back(local, monkeypatch):
    b = bundle(local)
    original = handoff.write_manifest

    def mutate_source(job, path, *, kind, approved, **kwargs):
        result = original(job, path, kind=kind, approved=approved, **kwargs)
        if kind == "cover_letter":
            config.RESUME_PATH.write_text("changed during import", encoding="utf-8")
        return result

    monkeypatch.setattr(handoff, "write_manifest", mutate_source)
    assert_rejected_clean(local, b)


def test_process_crash_before_commit_never_leaves_verifiable_approval(local):
    from applypilot.artifacts import verify_artifact

    b = bundle(local)
    before = job(local)
    script = r'''
import os
import sys
from pathlib import Path
from applypilot import config, database, local_handoff
root = Path(sys.argv[1])
config.APP_DIR = root
config.PROFILE_PATH = root / "profile.json"
config.RESUME_PATH = root / "resume.txt"
config.RESUME_PDF_PATH = root / "resume.pdf"
config.SEARCH_CONFIG_PATH = root / "searches.yaml"
config.DB_PATH = root / "jobs.db"
database.DB_PATH = config.DB_PATH
original = local_handoff.write_manifest
def crash_after_first(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(73)
local_handoff.write_manifest = crash_after_first
folder = Path(sys.argv[2])
local_handoff.import_result(folder / "handoff.json", folder / "result.json", folder / "review.json")
'''
    child = subprocess.run([sys.executable, "-c", script, str(local.private), str(b.folder)],
                           capture_output=True, text=True, timeout=30, check=False)
    assert child.returncode == 73, child.stderr
    assert job(local) == before
    registry = local.conn.execute("SELECT imported_at FROM local_preparation_handoffs").fetchone()
    assert registry[0] is None
    with pytest.raises(ValueError):
        verify_artifact(before, b.folder / "resume-tailored.txt", kind="resume")


def test_committed_import_artifacts_verify_against_receipt(local):
    from applypilot.artifacts import verify_job_artifacts

    b = bundle(local)
    import_bundle(b)
    assert verify_job_artifacts(job(local)) == {
        "resume": b.folder / "resume-tailored.pdf", "cover_letter": b.folder / "cover-letter.pdf"}


@pytest.mark.parametrize("mutation", ["strip_marker", "change_receipt", "clear_import", "wrong_job"])
def test_committed_receipt_cannot_be_bypassed(local, mutation):
    from applypilot.artifacts import verify_artifact

    b = bundle(local)
    import_bundle(b)
    if mutation == "strip_marker":
        path = b.folder / "resume-tailored.manifest.json"
        manifest = handoff.read(path)
        manifest.pop("local_preparation")
        write_json(path, manifest)
    elif mutation == "change_receipt":
        row = local.conn.execute("SELECT receipt_json FROM local_preparation_handoffs").fetchone()
        receipt = json.loads(row[0])
        receipt["reviewer"] = "another-reviewer"
        local.conn.execute("UPDATE local_preparation_handoffs SET receipt_json=?", (json.dumps(receipt),))
    elif mutation == "clear_import":
        local.conn.execute("UPDATE local_preparation_handoffs SET imported_at=NULL")
    else:
        local.conn.execute("UPDATE local_preparation_handoffs SET job_url='https://other.example.test/job'")
    local.conn.commit()
    with pytest.raises(ValueError):
        verify_artifact(job(local), b.folder / "resume-tailored.txt", kind="resume")


@pytest.mark.parametrize("strip_marker", [False, True])
def test_local_pdf_regeneration_cannot_refresh_prior_review(local, strip_marker):
    from applypilot.artifacts import refresh_pdf_manifest

    b = bundle(local)
    import_bundle(b)
    path = b.folder / "resume-tailored.manifest.json"
    if strip_marker:
        manifest = handoff.read(path)
        manifest.pop("local_preparation")
        write_json(path, manifest)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        refresh_pdf_manifest(b.folder / "resume-tailored.txt")
    assert path.read_bytes() == before
