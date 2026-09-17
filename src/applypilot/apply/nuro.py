"""Exact Nuro 8187498 question mapping and an offline, synthetic form exercise.

This module has no employer submission transport. Its confirmation belongs to a
locally generated fixture and must never be interpreted as an application.
Public Greenhouse question metadata was reviewed on 2026-09-17.
"""

import hashlib
import html
import json
import math
import socket
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

from applypilot.apply.archer import profile_value, verified_file_payloads
from applypilot.apply.gate import PreparedAdapter, validate_payload

JOB_URL = "https://nuro.ai/careersitem?gh_jid=8187498"
FORM_URL = "https://job-boards.greenhouse.io/embed/job_app?for=nuro&token=8187498"
METADATA_URL = "https://boards-api.greenhouse.io/v1/boards/nuro/jobs/8187498?questions=true"
COMPANY = "Nuro"
TITLE = "AV Hardware Mechanical Engineer"
POSTING_ID = 8187498
REQUISITION_ID = "R-103101"
HYBRID_QUESTION = (
    "This position is hybrid and requires 4 days a week in office, including Thursdays in our Mountain View, "
    "CA headquarters and the remaining 3 days in either Mountain View or our San Francisco, CA office. "
    "Are you able to meet this requirement?"
)
# name: (exact employer label, profile source, required, public API field type)
FIELDS = {
    "first_name": ("First Name", "personal.first_name", True, "input_text"),
    "last_name": ("Last Name", "personal.last_name", True, "input_text"),
    "email": ("Email", "personal.email", True, "input_text"),
    "phone": ("Phone", "personal.phone", True, "input_text"),
    "question_69052553": ("LinkedIn Profile", "personal.linkedin_url", False, "input_text"),
    "question_69052554": ("Website", "personal.portfolio_url", False, "input_text"),
    "question_69052555": ("Are you authorized to work in the country in which you are applying?",
                          "work_authorization.legally_authorized_to_work", True, "multi_value_single_select"),
    "question_69052556": (
        "Do you now, or will you in the future, require sponsorship for employment in the country which you are applying?",
        "work_authorization.require_sponsorship", True, "multi_value_single_select"),
    "question_69052557": (HYBRID_QUESTION, "employer_answers.nuro.hybrid_schedule_confirmed",
                          True, "multi_value_single_select"),
}
YES_NO = [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]
OPTIONAL_EEO = {
    "disability_status": ("DisabilityStatus", [
        ("I do not want to answer", "3"),
        ("No, I do not have a disability and have not had one in the past", "2"),
        ("Yes, I have a disability, or have had one in the past", "1")]),
    "veteran_status": ("VeteranStatus", [
        ("I don't wish to answer", "3"),
        ("I identify as one or more of the classifications of a protected veteran", "2"),
        ("I am not a protected veteran", "1")]),
    "race": ("Race", [("Decline To Self Identify", "8"), ("Two or More Races", "7"),
                       ("Native Hawaiian or Other Pacific Islander", "6"), ("White", "5"),
                       ("Hispanic or Latino", "4"), ("Black or African American", "3"),
                       ("Asian", "2"), ("American Indian or Alaskan Native", "1")]),
    "gender": ("Gender", [("Decline To Self Identify", "3"), ("Female", "2"), ("Male", "1")]),
}


def _signature(question: dict) -> dict:
    if not isinstance(question, dict) or type(question.get("required")) is not bool:
        raise ValueError("Malformed Nuro question")
    return {key: question.get(key) for key in ("label", "required", "fields")}


def reviewed_questions() -> list[dict]:
    """Return the reviewed public field schema, also useful for local fixtures."""
    questions = [{"label": label, "required": required,
                  "fields": [{"name": name, "type": kind,
                              "values": [dict(v) for v in YES_NO] if "select" in kind else []}]}
                 for name, (label, _, required, kind) in FIELDS.items()]
    for name, label, required in [("resume", "Resume/CV", True), ("cover_letter", "Cover Letter", False)]:
        questions.append({"label": label, "required": required, "fields": [
            {"name": name, "type": "input_file", "values": []},
            {"name": name + "_text", "type": "textarea", "values": []}]})
    return questions


def validate_metadata(metadata: dict) -> str:
    """Reject changed identity, questions, answer values, requiredness or consent.

    Voluntary demographics are checked but never filled by this adapter. The
    returned digest binds the entire supplied capture, including legal prose.
    """
    try:
        if (type(metadata["id"]) is not int or metadata["id"] != POSTING_ID
                or metadata["requisition_id"] != REQUISITION_ID
                or metadata["company_name"] != COMPANY or metadata["title"] != TITLE
                or metadata["absolute_url"] != JOB_URL):
            raise ValueError("Nuro metadata job identity changed")
        actual = [_signature(q) for q in metadata["questions"]]
        expected = reviewed_questions()
        def sort(xs):
            return json.dumps(sorted(xs, key=lambda q: q["label"]), sort_keys=True)
        if sort(actual) != sort(expected):
            raise ValueError("Nuro application questions changed")
        location = [{"label": label, "required": True,
                     "fields": [{"name": name, "type": kind, "values": []}]}
                    for name, label, kind in [("longitude", "Longitude", "input_hidden"),
                                              ("latitude", "Latitude", "input_hidden"),
                                              ("location", "Location", "input_text")]]
        if sort([_signature(q) for q in metadata["location_questions"]]) != sort(location):
            raise ValueError("Nuro location questions changed")
        eeo = [{"label": label, "required": False, "fields": [{"name": name,
                "type": "multi_value_single_select", "values": [{"label": a, "value": b} for a, b in options]}]}
               for name, (label, options) in OPTIONAL_EEO.items()]
        groups = metadata["compliance"]
        if (len(groups) != 4 or any(g["type"] != "eeoc" for g in groups)
                or sort([_signature(q) for g in groups for q in g["questions"]]) != sort(eeo)
                or metadata["demographic_questions"] is not None):
            raise ValueError("Nuro voluntary questions changed")
        compliance = [{"type": "gdpr", "requires_consent": False,
                "requires_processing_consent": False, "requires_retention_consent": False,
                "retention_period": None, "demographic_data_consent_applies": False}]
        if json.dumps(metadata["data_compliance"], sort_keys=True) != json.dumps(compliance, sort_keys=True):
            raise ValueError("Nuro consent requirements changed")
        return hashlib.sha256(json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Malformed Nuro public metadata") from exc


def prepare_fields(job: dict, profile: dict, metadata: dict) -> dict:
    """Map explicit answers only. No network, artifacts, database or inference."""
    if (job.get("url") != JOB_URL or job.get("company") != COMPANY or job.get("title") != TITLE
            or job.get("application_url") not in {JOB_URL, FORM_URL}):
        raise ValueError("Job does not match exact Nuro adapter")
    digest = validate_metadata(metadata)
    values, missing, provenance = {}, [], {}
    for name, (label, path, required, kind) in FIELDS.items():
        value = profile_value(profile, path)
        if kind == "multi_value_single_select" and value is not None:
            if value.casefold() not in {"yes", "no"}:
                raise ValueError(f"Explicit Yes/No answer required: {name}")
            value = "1" if value.casefold() == "yes" else "0"
        if value is None:
            if required:
                missing.append({"field": name, "question": label, "profile_path": path})
        else:
            values[name], provenance[name] = value, path
    dom_fields = {name: {"profile_path": path, "control": "combobox" if "select" in kind else "text"}
                  for name, (_, path, _, kind) in FIELDS.items()}
    dom_fields.update(country={"profile_path": "personal.country", "control": "combobox"},
                      **{"candidate-location": {"profile_path": "personal.city", "control": "geocoder_unqualified"}})
    return {"fields": values, "provenance": provenance, "missing_fields": missing,
            "public_dom_mapping": dom_fields,
            "metadata_sha256": digest, "voluntary_demographics": "left_blank",
            "unresolved_live_controls": ["Employer location autocomplete and hidden latitude/longitude",
                "Hydrated employer upload, CAPTCHA, submission transport and confirmation are unqualified"]}


def local_preview(job: dict, *, profile: dict, metadata: dict, output_dir: Path,
                  fixture_location: dict | None = None, fixture_outcome: str = "confirmed") -> dict:
    """Exercise mapped fields/files against a generated, network-denied fixture.

    fixture_location is explicitly synthetic test data; it never becomes a real
    application answer. Omit it for a factual preview with unresolved geocoding.
    The controlled upload is inspected in a Playwright route and fulfilled in
    memory; not even a localhost server receives the bytes. No live page is used.
    """
    if fixture_outcome not in {"confirmed", "unknown", "rejected"}:
        raise ValueError("Unsupported local fixture outcome")
    prepared = prepare_fields(job, profile, metadata)
    fields = dict(prepared["fields"])
    missing = list(prepared["missing_fields"])
    if fixture_location is not None:
        if (not isinstance(fixture_location, dict) or set(fixture_location) != {"label", "latitude", "longitude"}
                or not isinstance(fixture_location["label"], str) or not fixture_location["label"].strip()
                or any(type(fixture_location[k]) not in {int, float} or not math.isfinite(fixture_location[k])
                       for k in ("latitude", "longitude"))
                or not -90 <= fixture_location["latitude"] <= 90
                or not -180 <= fixture_location["longitude"] <= 180):
            raise ValueError("Invalid synthetic fixture location")
        fields.update(location=fixture_location["label"], latitude=str(fixture_location["latitude"]),
                      longitude=str(fixture_location["longitude"]))
    else:
        missing.append({"field": "location", "reason": "Location selection is unvalidated; coordinates are never invented"})
    files = {}
    try:
        files = verified_file_payloads(job)
    except ValueError as exc:
        missing.append({"field": "resume", "reason": str(exc)})
    fields["job_id"] = str(POSTING_ID)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    token = uuid.uuid4().hex
    with socket.socket() as fixture_address:
        fixture_address.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{fixture_address.getsockname()[1]}"
    application, submit = (origin + path for path in ("/fixture", "/fixture-upload"))
    confirmation = submit  # This controlled fixture returns its receipt directly in the POST response.
    adapter = PreparedAdapter(application, submit, (confirmation,), fields,
                              {k: v["sha256"] for k, v in files.items()}, COMPANY, TITLE)
    controls = []
    for name, value in fields.items():
        label = FIELDS[name][0] if name in FIELDS else name
        if name in FIELDS and FIELDS[name][3] == "multi_value_single_select":
            element = f'<select name="{name}"><option value=""></option><option value="1">Yes</option><option value="0">No</option></select>'
        else:
            element = f'<input name="{name}" type="text">'
        controls.append(f'<label>{html.escape(label)}{element}</label>')
    controls.extend(f'<label>{kind}<input name="{kind}" type="file"></label>' for kind in files)
    document = (f'<h1>LOCAL FIXTURE ONLY — {TITLE}</h1><p>{COMPANY}</p>'
                f'<form method="post" action="{submit}" enctype="multipart/form-data">'
                + "".join(controls) + '<button type="submit">Exercise local fixture</button></form>')
    evidence = {"adapter": "nuro_8187498_local_fixture_v1", "job_url": JOB_URL,
                "metadata_sha256": prepared["metadata_sha256"], "submitted": False,
                "live_submission_supported": False, "employer_upload_verified": False,
                "employer_confirmation_verified": False, "network_denied_before_candidate_input": True,
                "status": "needs_input", "missing_fields": missing,
                "filled_fields": prepared["provenance"], "fixture_location_is_synthetic": fixture_location is not None,
                "local_fixture_post_count": 0, "local_fixture_payload_validated": False,
                "local_fixture_confirmation_verified": False, "blocked_requests": [],
                "files": {k: {"sha256": v["sha256"], "bytes": len(v["payload"]["buffer"])} for k, v in files.items()},
                "limitations": prepared["unresolved_live_controls"] + [
                    "Generated native HTML fixture; real Greenhouse widgets and transport are not exercised",
                    "A local fixture result grants no permission or eligibility to apply"]}
    with socket.socket() as proxy, sync_playwright() as playwright:
        proxy.bind(("127.0.0.1", 0))
        browser = playwright.chromium.launch(headless=True,
            proxy={"server": f"http://127.0.0.1:{proxy.getsockname()[1]}"},
            args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--proxy-bypass-list=<-loopback>"])
        context = browser.new_context(service_workers="block", accept_downloads=False)
        csp = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-src 'none'"
        def route_request(route):
            request = route.request
            if request.url == application and request.method == "GET":
                route.fulfill(body=document, content_type="text/html", headers={"Content-Security-Policy": csp})
            elif request.url == submit and request.method == "POST" and not missing:
                evidence["local_fixture_post_count"] += 1
                try:
                    if evidence["local_fixture_post_count"] != 1:
                        raise ValueError("Duplicate local fixture POST")
                    validate_payload(request.headers.get("content-type", ""), request.post_data_buffer or b"", adapter)
                    evidence["local_fixture_payload_validated"] = True
                except ValueError as exc:
                    evidence["payload_error"] = str(exc)
                    route.fulfill(status=400, body="Local fixture rejected")
                    return
                if fixture_outcome == "confirmed":
                    route.fulfill(body=f'<h1>Local fixture receipt</h1><p id="receipt">{token}:{POSTING_ID}</p>',
                                  content_type="text/html", headers={"Content-Security-Policy": csp})
                else:
                    route.fulfill(status=400 if fixture_outcome == "rejected" else 200,
                                  body="Local fixture response without a receipt")
            else:
                evidence["blocked_requests"].append({"method": request.method, "url": request.url.split("?")[0]})
                route.abort("blockedbyclient")
        try:
            context.route("**/*", route_request)
            context.route_web_socket("**/*", lambda ws: ws.close())
            context.add_init_script("""for(const key of ['RTCPeerConnection','webkitRTCPeerConnection']) {
                Object.defineProperty(window,key,{value:undefined,writable:false,configurable:false}); }
                Object.defineProperty(window,'open',{value:()=>null,writable:false,configurable:false});""")
            page = context.new_page()
            context.on("page", lambda popup: popup.close() if popup != page else None)
            page.on("download", lambda download: download.cancel())
            page.on("dialog", lambda dialog: dialog.dismiss())
            page.goto(application)
            for name, value in fields.items():
                locator = page.locator(f'[name="{name}"]')
                if name in FIELDS and FIELDS[name][3] == "multi_value_single_select":
                    locator.select_option(value)
                else:
                    locator.fill(value)
            for kind, item in files.items():
                page.locator(f'[name="{kind}"]').set_input_files(item["payload"])
            page.screenshot(path=str(output / "local-form.png"), full_page=True)
            if not missing:
                page.get_by_role("button", name="Exercise local fixture").click()
                page.wait_for_load_state("load")
                confirmed = (evidence["local_fixture_payload_validated"] and evidence["local_fixture_post_count"] == 1
                             and page.url == confirmation and page.locator("#receipt").count() == 1
                             and page.locator("#receipt").inner_text() == f"{token}:{POSTING_ID}")
                evidence["local_fixture_confirmation_verified"] = confirmed
                evidence["status"] = "local_fixture_passed" if confirmed else "local_fixture_unconfirmed"
        finally:
            browser.close()
            (output / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    return evidence
