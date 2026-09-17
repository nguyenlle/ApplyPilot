"""Adverse contract and in-memory browser transport checks; no employer calls."""

import copy
import json

import pytest

from applypilot.apply import nuro
from applypilot.artifacts import write_manifest


@pytest.fixture
def metadata():
    return {
        "id": 8187498, "requisition_id": "R-103101", "company_name": "Nuro",
        "title": "AV Hardware Mechanical Engineer", "absolute_url": nuro.JOB_URL,
        "questions": nuro.reviewed_questions(),
        "location_questions": [{"label": label, "required": True,
            "fields": [{"name": name, "type": kind, "values": []}]}
            for name, label, kind in [("longitude", "Longitude", "input_hidden"),
                                     ("latitude", "Latitude", "input_hidden"),
                                     ("location", "Location", "input_text")]],
        "compliance": [{"type": "eeoc", "questions": [{"label": label, "required": False,
            "fields": [{"name": name, "type": "multi_value_single_select",
                        "values": [{"label": a, "value": b} for a, b in options]}]}]}
            for name, (label, options) in nuro.OPTIONAL_EEO.items()],
        "demographic_questions": None,
        "data_compliance": [{"type": "gdpr", "requires_consent": False,
            "requires_processing_consent": False, "requires_retention_consent": False,
            "retention_period": None, "demographic_data_consent_applies": False}],
    }


@pytest.fixture
def job():
    return {"url": nuro.JOB_URL, "application_url": nuro.JOB_URL,
            "company": nuro.COMPANY, "title": nuro.TITLE}


@pytest.fixture
def profile():
    return {"personal": {"first_name": "Synthetic", "last_name": "Applicant",
                "email": "synthetic@example.test", "phone": "+12025550100", "country": "United States"},
            "work_authorization": {"legally_authorized_to_work": True, "require_sponsorship": "Yes"},
            "employer_answers": {"nuro": {"hybrid_schedule_confirmed": True}}}


def documents(job, directory):
    for kind, field in [("resume", "tailored_resume_path"), ("cover_letter", "cover_letter_path")]:
        text = directory / f"{kind}.txt"
        text.write_text(f"Synthetic reviewed {kind}")
        text.with_suffix(".pdf").write_bytes(b"%PDF-1.4 local synthetic bytes")
        write_manifest(job, text, kind=kind, approved=True)
        job[field] = str(text)


def test_explicit_mapping_and_optional_sensitive_answers_remain_blank(job, profile, metadata):
    profile["eeo_voluntary"] = {"gender": "Female", "race": "Asian", "disability_status": "No"}
    value = nuro.prepare_fields(job, profile, metadata)
    assert value["fields"]["question_69052555"] == "1"
    assert value["fields"]["question_69052556"] == "1"
    assert value["fields"]["question_69052557"] == "1"
    assert not value["missing_fields"]
    assert not set(value["fields"]) & set(nuro.OPTIONAL_EEO)
    assert "latitude" not in value["fields"]
    assert value["unresolved_live_controls"]


@pytest.mark.parametrize("key,value", [("id", True), ("id", "8187498"), ("requisition_id", "R-other"),
    ("company_name", "Other"), ("title", "Another role"), ("absolute_url", "https://example.test/job")])
def test_metadata_identity_mismatch_rejected(metadata, key, value):
    metadata[key] = value
    with pytest.raises(ValueError):
        nuro.validate_metadata(metadata)


@pytest.mark.parametrize("change", ["label", "required", "required_type", "option", "option_type", "name", "type",
    "missing", "extra", "duplicate", "location", "eeo_required", "eeo_option", "consent", "consent_type", "demographic"])
def test_changed_public_contract_fails_closed(metadata, change):
    q = next(q for q in metadata["questions"] if q["fields"][0]["name"] == "question_69052555")
    if change in {"label", "required", "required_type"}:
        q[{"required_type": "required"}.get(change, change)] = {
            "label": "Do you consent?", "required": False, "required_type": 1}[change]
    elif change == "option":
        q["fields"][0]["values"][0]["value"] = 2
    elif change == "option_type":
        q["fields"][0]["values"][0]["value"] = True
    elif change in {"name", "type"}:
        q["fields"][0][change] = "unknown"
    elif change == "missing":
        metadata["questions"].pop()
    elif change in {"extra", "duplicate"}:
        metadata["questions"].append(copy.deepcopy(q))
    elif change == "location":
        metadata["location_questions"][0]["required"] = False
    elif change == "eeo_required":
        metadata["compliance"][0]["questions"][0]["required"] = True
    elif change == "eeo_option":
        metadata["compliance"][0]["questions"][0]["fields"][0]["values"].pop()
    elif change.startswith("consent"):
        metadata["data_compliance"][0]["requires_consent"] = True if change == "consent" else 0
    else:
        metadata["demographic_questions"] = []
    with pytest.raises(ValueError):
        nuro.validate_metadata(metadata)


@pytest.mark.parametrize("metadata", [None, [], {}, {"id": 8187498}])
def test_malformed_metadata_rejected(metadata):
    with pytest.raises(ValueError):
        nuro.validate_metadata(metadata)


@pytest.mark.parametrize("key", ["url", "application_url", "company", "title"])
def test_wrong_queued_job_rejected(job, profile, metadata, key):
    job[key] = "other"
    with pytest.raises(ValueError, match="exact Nuro"):
        nuro.prepare_fields(job, profile, metadata)


@pytest.mark.parametrize("answer", [None, "", "unknown", {}, []])
def test_general_hybrid_preference_does_not_answer_specific_schedule(job, profile, metadata, answer):
    profile["employer_answers"]["nuro"]["hybrid_schedule_confirmed"] = answer
    profile["job_preferences"] = {"work_modes": ["hybrid", "onsite"]}
    out = nuro.prepare_fields(job, profile, metadata)
    assert [x["field"] for x in out["missing_fields"]] == ["question_69052557"]
    assert "question_69052557" not in out["fields"]


@pytest.mark.parametrize("answer", ["maybe", 1, 0, float("nan")])
def test_non_boolean_legal_answers_rejected(job, profile, metadata, answer):
    profile["work_authorization"]["require_sponsorship"] = answer
    with pytest.raises(ValueError, match="Yes/No"):
        nuro.prepare_fields(job, profile, metadata)


def test_metadata_digest_binds_changed_legal_prose(metadata):
    before = nuro.validate_metadata(metadata)
    metadata["compliance"][0]["description"] = "Revised voluntary survey explanation"
    assert nuro.validate_metadata(metadata) != before


@pytest.mark.browser
@pytest.mark.parametrize("outcome", ["confirmed", "unknown", "rejected"])
def test_local_fixture_exact_files_payload_and_confirmation(job, profile, metadata, tmp_path, outcome):
    documents(job, tmp_path)
    job_before, profile_before = copy.deepcopy(job), copy.deepcopy(profile)
    result = nuro.local_preview(job, profile=profile, metadata=metadata, output_dir=tmp_path / "preview",
        fixture_location={"label": "Synthetic fixture city", "latitude": 1.0, "longitude": 2.0},
        fixture_outcome=outcome)
    assert result["status"] == ("local_fixture_passed" if outcome == "confirmed" else "local_fixture_unconfirmed")
    assert result["local_fixture_post_count"] == 1
    assert result["local_fixture_payload_validated"] is True
    assert result["local_fixture_confirmation_verified"] is (outcome == "confirmed")
    assert result["submitted"] is result["live_submission_supported"] is False
    assert result["employer_upload_verified"] is result["employer_confirmation_verified"] is False
    assert result["network_denied_before_candidate_input"] is True
    assert result["fixture_location_is_synthetic"] is True
    assert job == job_before and profile == profile_before
    assert (tmp_path / "preview/local-form.png").is_file()
    assert json.loads((tmp_path / "preview/evidence.json").read_text()) == result


@pytest.mark.browser
@pytest.mark.parametrize("missing", ["answer", "location", "resume", "changed_pdf", "wrong_job_artifact"])
def test_missing_or_unapproved_data_never_triggers_fixture_post(job, profile, metadata, tmp_path, missing):
    documents(job, tmp_path)
    location = {"label": "Synthetic fixture city", "latitude": 1, "longitude": 2}
    if missing == "answer":
        profile["employer_answers"]["nuro"]["hybrid_schedule_confirmed"] = None
    elif missing == "location":
        location = None
    elif missing == "resume":
        del job["tailored_resume_path"]
    elif missing == "changed_pdf":
        (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4 modified")
    else:
        manifest = tmp_path / "resume.manifest.json"
        doc = json.loads(manifest.read_text())
        doc["job_url"] = "https://example.test/other-job"
        manifest.write_text(json.dumps(doc))
    result = nuro.local_preview(job, profile=profile, metadata=metadata,
                                output_dir=tmp_path / "preview", fixture_location=location)
    assert result["status"] == "needs_input"
    assert result["local_fixture_post_count"] == 0
    assert result["local_fixture_confirmation_verified"] is result["submitted"] is False


@pytest.mark.parametrize("location", [[], {}, {"label": "x", "latitude": True, "longitude": 2},
    {"label": "x", "latitude": 91, "longitude": 2}, {"label": "x", "latitude": 1, "longitude": float("nan")}])
def test_invalid_fixture_coordinates_rejected_before_browser(job, profile, metadata, tmp_path, location):
    with pytest.raises(ValueError, match="synthetic fixture"):
        nuro.local_preview(job, profile=profile, metadata=metadata, output_dir=tmp_path / "out", fixture_location=location)
