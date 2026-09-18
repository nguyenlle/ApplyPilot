import copy
import hashlib
import json
import socket
from concurrent.futures import ThreadPoolExecutor

import pytest

from applypilot.apply import nuro_transport as transport

INIT_URL = transport.INITIALIZER_URL + "?fields%5B%5D=resume&fields%5B%5D=cover_letter"


@pytest.fixture
def config():
    docs = {}
    for kind in ("resume", "cover_letter"):
        content = b"%PDF-1.7 synthetic " + kind.encode()
        docs[kind] = {"name": kind + ".pdf", "content": content,
                      "sha256": hashlib.sha256(content).hexdigest()}
    return {"approved_fields": {"first_name": "Example", "last_name": "Applicant", "email": "example@example.test",
                "phone": "+12025550100", "question_69052553": "https://example.test/profile",
                "question_69052555": "1", "question_69052556": "1", "question_69052557": "1"},
            "documents": docs,
            "location": {"location": "Example City, California, United States", "latitude": "37.0",
                         "longitude": "-122.0", "country_short_name": "US"},
            "reviewed_storage_url": transport.STORAGE_URL, "time_zone": "America/Los_Angeles",
            "reserve_submission": lambda _: None}


@pytest.fixture
def initializer():
    return {"url": transport.STORAGE_URL, **{kind: {
        "fields": {key: "synthetic-" + key for key in transport.SIGNED_FIELDS},
        "key": "stash/applications/resumes/{timestamp}-{unique_id}-" + character * 32}
        for kind, character in [("resume", "a"), ("cover_letter", "b")]}}


def key_for(initializer, kind):
    return initializer[kind]["key"].replace("{timestamp}", "1789650000000").replace("{unique_id}", "fixture1")


def multipart(config, initializer, kind, *, scalars=None, file=None, filename=None, extra=None):
    scalar_fields = {**initializer[kind]["fields"], "utf8": "✓", "key": key_for(initializer, kind),
                     "authenticity_token": "1234", "Content-Type": "application/octet-stream"}
    if scalars is not None:
        scalar_fields = scalars
    parts = []
    for name, value in scalar_fields.items():
        parts.append(f'--boundary\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    filename = filename if filename is not None else config["documents"][kind]["name"]
    data = file if file is not None else config["documents"][kind]["content"]
    parts.append(f'--boundary\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'.encode() + data + b"\r\n")
    if extra:
        parts.append(extra)
    parts.append(b"--boundary--\r\n")
    return "multipart/form-data; boundary=boundary", b"".join(parts)


def ready(config, initializer):
    controller = transport.NuroTransport(**config)
    controller.bind_initializer(INIT_URL, initializer)
    for kind in ("resume", "cover_letter"):
        content_type, body = multipart(config, initializer, kind)
        ticket = controller.validate_upload(transport.STORAGE_URL, content_type, body)
        controller.mark_upload_response(ticket, 201)
    return controller


def application(config, initializer):
    # Explicit source-derived JSON shape, independent of validator construction.
    f = config["approved_fields"]
    result = {k: f[k] for k in ("first_name", "last_name", "email", "phone")}
    result.update(config["location"])
    result.update(answers_attributes={
        "69052553": {"question_id": "69052553", "priority": 0, "text_value": f["question_69052553"]},
        "69052554": {"question_id": "69052554", "priority": 1},
        "69052555": {"question_id": "69052555", "priority": 2, "boolean_value": 1},
        "69052556": {"question_id": "69052556", "priority": 3, "boolean_value": 1},
        "69052557": {"question_id": "69052557", "priority": 4, "boolean_value": 1}},
        demographic_answers=[], data_compliance={}, attachments={}, from_job_board_renderer=True,
        employments=[], mapped_url_token=None, appcast_click_id=None, time_zone="America/Los_Angeles")
    for kind in ("resume", "cover_letter"):
        result[kind + "_url"] = transport.STORAGE_URL + "/" + key_for(initializer, kind)
        result[kind + "_url_filename"] = kind + ".pdf"
    return {"job_application": result, "fingerprint": "synthetic-opaque-fingerprint",
            "g-recaptcha-enterprise-token": "synthetic-opaque-proof"}


def final(controller, payload, url=transport.SUBMISSION_URL):
    return controller.reserve_final(url, "application/json", json.dumps(payload).encode())


def test_exact_uploaded_bytes_references_and_facts_reserve_once(config, initializer):
    reservations = []
    config["reserve_submission"] = reservations.append
    controller = ready(config, initializer)
    payload = application(config, initializer)
    proof = final(controller, payload)
    assert reservations == [proof["request_sha256"]]
    assert proof["posting_id"] == 8187498 and proof["employer_confirmation_verified"] is False
    assert "synthetic-opaque" not in json.dumps(proof)
    with pytest.raises(ValueError, match="already reserved"):
        final(controller, payload)
    assert len(reservations) == 1


@pytest.mark.parametrize("change", ["extra_fact", "missing_fact", "bad_answer", "non_string", "storage",
    "country", "coordinate", "coordinate_nan", "filename", "document_digest", "document_bytes", "document_missing"])
def test_unapproved_constructor_inputs_rejected(config, change):
    if change == "extra_fact":
        config["approved_fields"]["consent"] = "yes"
    elif change == "missing_fact":
        del config["approved_fields"]["question_69052557"]
    elif change == "bad_answer":
        config["approved_fields"]["question_69052556"] = "Yes"
    elif change == "non_string":
        config["approved_fields"]["question_69052556"] = True
    elif change == "storage":
        config["reviewed_storage_url"] = "https://other.s3.amazonaws.com"
    elif change == "country":
        config["location"]["country_short_name"] = "CA"
    elif change.startswith("coordinate"):
        config["location"]["latitude"] = "91" if change == "coordinate" else "nan"
    elif change == "filename":
        config["documents"]["resume"]["name"] = "../resume.pdf"
    elif change == "document_digest":
        config["documents"]["resume"]["sha256"] = "0" * 64
    elif change == "document_bytes":
        config["documents"]["resume"]["content"] = bytearray(b"%PDF-mutable")
    else:
        del config["documents"]["cover_letter"]
    with pytest.raises(ValueError):
        transport.NuroTransport(**config)


@pytest.mark.parametrize("change", ["origin", "path", "port", "http", "extra_query", "duplicate_query", "malformed_query",
    "redirect", "status_bool", "response_extra", "fields_extra", "fields_missing", "key_escape", "key_suffix", "storage"])
def test_initializer_provenance_and_shape_rejected(config, initializer, change):
    controller = transport.NuroTransport(**config)
    url, status = INIT_URL, 200
    if change == "origin":
        url = url.replace("boards.greenhouse.io", "boards.greenhouse.io.example.test")
    elif change == "path":
        url = url.replace("presigned_fields", "other")
    elif change == "port":
        url = url.replace("boards.greenhouse.io", "boards.greenhouse.io:443")
    elif change == "http":
        url = url.replace("https", "http")
    elif change == "extra_query":
        url += "&job=other"
    elif change == "duplicate_query":
        url += "&fields[]=resume"
    elif change == "malformed_query":
        url += "&malformed"
    elif change == "redirect":
        status = 302
    elif change == "status_bool":
        status = True
    elif change == "response_extra":
        initializer["other_document"] = {}
    elif change == "fields_extra":
        initializer["resume"]["fields"]["redirect"] = "https://other.test"
    elif change == "fields_missing":
        initializer["resume"]["fields"].pop("policy")
    elif change == "key_escape":
        initializer["resume"]["key"] = initializer["resume"]["key"].replace("resumes/", "resumes/../")
    elif change == "key_suffix":
        initializer["resume"]["key"] += "x"
    else:
        initializer["url"] = transport.STORAGE_URL + "/evil"
    with pytest.raises(ValueError):
        controller.bind_initializer(url, initializer, status=status)


@pytest.mark.parametrize("change", ["bytes", "filename", "wrong_kind", "policy", "extra_scalar", "duplicate_scalar",
    "duplicate_file", "key_sibling", "key_escape", "content_type_case", "destination", "encoding", "transfer_encoding"])
def test_wrong_or_ambiguous_upload_never_receives_ticket(config, initializer, change):
    controller = transport.NuroTransport(**config)
    controller.bind_initializer(INIT_URL, initializer)
    mime, body = multipart(config, initializer, "resume")
    url = transport.STORAGE_URL
    if change == "bytes":
        body = body.replace(b"synthetic resume", b"modified resume")
    elif change == "filename":
        body = body.replace(b'filename="resume.pdf"', b'filename="other.pdf"')
    elif change == "wrong_kind":
        body = body.replace(key_for(initializer, "resume").encode(), key_for(initializer, "cover_letter").encode())
    elif change == "policy":
        body = body.replace(b"synthetic-policy", b"modified-policy")
    elif change in {"extra_scalar", "duplicate_scalar", "duplicate_file"}:
        name = "email" if change == "extra_scalar" else "utf8" if change == "duplicate_scalar" else "file"
        filename = '; filename="second.pdf"' if change == "duplicate_file" else ""
        extra = f'--boundary\r\nContent-Disposition: form-data; name="{name}"{filename}\r\n\r\nunapproved\r\n'.encode()
        mime, body = multipart(config, initializer, "resume", extra=extra)
    elif change == "key_sibling":
        body = body.replace(b"a" * 32, b"c" * 32)
    elif change == "key_escape":
        body = body.replace(b"1789650000000-fixture1", b"1789650000000-../escape")
    elif change == "content_type_case":
        body = body.replace(b'name="Content-Type"', b'name="content-type"')
    elif change == "destination":
        url += "/other"
    elif change == "encoding":
        mime = "application/json"
    else:
        body = body.replace(b"Content-Type: application/pdf\r\n", b"Content-Type: application/pdf\r\nContent-Transfer-Encoding: base64\r\n")
    with pytest.raises(ValueError):
        controller.validate_upload(url, mime, body)


def test_documents_and_initializer_are_frozen_against_caller_mutation(config, initializer):
    original_config, original_init = copy.deepcopy(config), copy.deepcopy(initializer)
    controller = transport.NuroTransport(**config)
    controller.bind_initializer(INIT_URL, initializer)
    initializer["resume"]["fields"]["policy"] = "tampered"
    config["documents"]["resume"]["content"] = b"%PDF-changed"
    mime, body = multipart(original_config, original_init, "resume")
    assert controller.validate_upload(transport.STORAGE_URL, mime, body)


@pytest.mark.parametrize("change", ["disposition_header", "type_header", "filename", "name", "boundary"])
def test_duplicate_mime_headers_and_parameters_are_rejected(config, initializer, change):
    controller = transport.NuroTransport(**config)
    controller.bind_initializer(INIT_URL, initializer)
    mime, body = multipart(config, initializer, "resume")
    if change == "disposition_header":
        body = body.replace(b'Content-Type: application/pdf',
                            b'Content-Disposition: form-data; name="file"; filename="evil.pdf"\r\nContent-Type: application/pdf')
    elif change == "type_header":
        body = body.replace(b'Content-Type: application/pdf', b'Content-Type: text/plain\r\nContent-Type: application/pdf')
    elif change == "filename":
        body = body.replace(b'filename="resume.pdf"', b'filename="resume.pdf"; filename="evil.pdf"')
    elif change == "name":
        body = body.replace(b'name="file"', b'name="file"; name="evil"')
    else:
        mime += "; boundary=other"
    with pytest.raises(ValueError):
        controller.validate_upload(transport.STORAGE_URL, mime, body)


def test_two_document_initializers_cannot_share_object_key(config, initializer):
    initializer["cover_letter"]["key"] = initializer["resume"]["key"]
    controller = transport.NuroTransport(**config)
    with pytest.raises(ValueError, match="distinct objects"):
        controller.bind_initializer(INIT_URL, initializer)


@pytest.mark.parametrize("change", ["preamble", "epilogue", "unknown_header", "unknown_disposition_param",
                                   "unknown_type_param", "wrong_file_type"])
def test_unapproved_multipart_metadata_and_extra_wire_bytes_rejected(config, initializer, change):
    controller = transport.NuroTransport(**config)
    controller.bind_initializer(INIT_URL, initializer)
    mime, body = multipart(config, initializer, "resume")
    if change == "preamble":
        body = b"unapproved facts\r\n" + body
    elif change == "epilogue":
        body += b"unapproved facts"
    elif change == "unknown_header":
        body = body.replace(b"Content-Type: application/pdf", b"X-Private-Fact: forbidden\r\nContent-Type: application/pdf")
    elif change == "unknown_disposition_param":
        body = body.replace(b'filename="resume.pdf"', b'filename="resume.pdf"; fact="unapproved"')
    elif change == "unknown_type_param":
        mime += "; fact=unapproved"
    else:
        body = body.replace(b"Content-Type: application/pdf", b"Content-Type: text/plain")
    with pytest.raises(ValueError):
        controller.validate_upload(transport.STORAGE_URL, mime, body)


@pytest.mark.parametrize("approved,wire,accepted", [
    ("202-555-0100", "+12025550100", True),
    ("1 (202) 555-0100", "+12025550100", True),
    ("202-555-0100", "+12025550101", False),
    ("202-555-0100 ext 4", "+12025550100", False),
    ("202-555-0100", "+442025550100", False),
])
def test_phone_widget_normalization_preserves_exact_approved_number(config, initializer, approved, wire, accepted):
    config["approved_fields"]["phone"] = approved
    controller = ready(config, initializer)
    payload = application(config, initializer)
    payload["job_application"]["phone"] = wire
    if accepted:
        assert final(controller, payload)["request_validated"]
    else:
        with pytest.raises(ValueError):
            final(controller, payload)


@pytest.mark.parametrize("status", [200, 201, 204, 400, 403, 500])
def test_only_successful_uploads_bind_final_references_and_no_retry(config, initializer, status):
    controller = transport.NuroTransport(**config)
    controller.bind_initializer(INIT_URL, initializer)
    for kind in ("resume", "cover_letter"):
        mime, body = multipart(config, initializer, kind)
        ticket = controller.validate_upload(transport.STORAGE_URL, mime, body)
        controller.mark_upload_response(ticket, status)
        with pytest.raises(ValueError):
            controller.mark_upload_response(ticket, 201)
        with pytest.raises(ValueError, match="already reserved"):
            controller.validate_upload(transport.STORAGE_URL, mime, body)
    if status < 300:
        assert final(controller, application(config, initializer))["submission_reserved"]
    else:
        with pytest.raises(ValueError, match="Both exact uploads"):
            final(controller, application(config, initializer))


def test_missing_response_cannot_be_promoted_or_rearmed(config, initializer):
    controller = transport.NuroTransport(**config)
    controller.bind_initializer(INIT_URL, initializer)
    mime, body = multipart(config, initializer, "resume")
    controller.validate_upload(transport.STORAGE_URL, mime, body)  # Simulated network timeout.
    with pytest.raises(ValueError):
        controller.validate_upload(transport.STORAGE_URL, mime, body)
    with pytest.raises(ValueError):
        controller.bind_initializer(INIT_URL, initializer)
    with pytest.raises(ValueError):
        controller.mark_upload_response("foreign-ticket", 201)
    with pytest.raises(ValueError):
        final(controller, application(config, initializer))


@pytest.mark.parametrize("change", ["email", "authorization", "sponsorship", "hybrid", "boolean_type", "question_id",
    "priority", "location", "coordinate", "country", "file_url", "file_name", "file_kind_swap", "extra_application",
    "resume_text", "eeo", "consent", "attachments", "employment", "education", "tracking", "timezone", "renderer",
    "top_level", "security_code", "captcha_retry", "security_object", "security_empty", "wrong_job"])
def test_changed_final_payload_never_consumes_reservation(config, initializer, change):
    calls = []
    config["reserve_submission"] = calls.append
    controller = ready(config, initializer)
    payload = application(config, initializer)
    app = payload["job_application"]
    url = transport.SUBMISSION_URL
    if change == "email":
        app["email"] = "other@example.test"
    elif change in {"authorization", "sponsorship", "hybrid", "boolean_type"}:
        key = {"authorization": "69052555", "sponsorship": "69052556", "hybrid": "69052557", "boolean_type": "69052556"}[change]
        app["answers_attributes"][key]["boolean_value"] = True if change == "boolean_type" else 0
    elif change in {"question_id", "priority"}:
        app["answers_attributes"]["69052557"][change] = "other" if change == "question_id" else 0
    elif change in {"location", "coordinate", "country"}:
        app[{"location": "location", "coordinate": "latitude", "country": "country_short_name"}[change]] = "other"
    elif change == "file_url":
        app["resume_url"] += "changed"
    elif change == "file_name":
        app["resume_url_filename"] = "other.pdf"
    elif change == "file_kind_swap":
        app["resume_url"], app["cover_letter_url"] = app["cover_letter_url"], app["resume_url"]
    elif change == "extra_application":
        app["job_id"] = "another-job"
    elif change == "resume_text":
        app["resume_text"] = "unapproved prose"
    elif change == "eeo":
        app["gender"] = "1"
    elif change == "consent":
        app["data_compliance"] = {"gdpr_consent_given": True}
    elif change == "attachments":
        app["attachments"] = {"other_url": "https://other.test"}
    elif change == "employment":
        app["employments"] = [{"company_name": "Invented"}]
    elif change == "education":
        app["educations"] = []
    elif change == "tracking":
        app["mapped_url_token"] = "unexpected"
    elif change == "timezone":
        app["time_zone"] = "Europe/London"
    elif change == "renderer":
        app["from_job_board_renderer"] = 1
    elif change in {"top_level", "security_code", "captcha_retry"}:
        payload[{"top_level": "other", "security_code": "security_code", "captcha_retry": "captcha_retried"}[change]] = True
    elif change == "security_object":
        payload["fingerprint"] = {"extra_facts": "not opaque"}
    elif change == "security_empty":
        payload["fingerprint"] = ""
    else:
        url = url.replace("8187498", "8187499")
    with pytest.raises(ValueError):
        final(controller, payload, url)
    assert not calls


def test_permitted_empty_renderer_variants_add_no_facts(config, initializer):
    controller = ready(config, initializer)
    payload = application(config, initializer)
    payload["job_application"]["employments"] = [copy.deepcopy(transport.EMPTY_EMPLOYMENT)]
    payload["job_application"]["educations"] = [copy.deepcopy(transport.EMPTY_EDUCATION)]
    payload["job_application"]["answers_attributes"]["69052554"]["text_value"] = ""
    assert final(controller, payload)["request_validated"]


@pytest.mark.parametrize("change", ["school", "degree", "discipline", "start_month", "start_year",
    "end_month", "end_year", "missing_field", "missing_nested", "extra_field", "duplicate", "empty",
    "null", "not_array", "bool_instead_of_null"])
def test_education_placeholder_accepts_no_facts_or_shape_changes(config, initializer, change):
    controller = ready(config, initializer)
    payload = application(config, initializer)
    entry = copy.deepcopy(transport.EMPTY_EDUCATION)
    education = [entry]
    if change in {"school", "degree", "discipline"}:
        entry[{"school": "school_name_id", "degree": "degree_id", "discipline": "discipline_id"}[change]] = "1"
    elif change.startswith(("start_", "end_")):
        side, field = change.split("_")
        entry[side + "_date"][field] = "1"
    elif change == "missing_field":
        del entry["degree_id"]
    elif change == "missing_nested":
        del entry["start_date"]["year"]
    elif change == "extra_field":
        entry["school_name"] = "Unapproved"
    elif change == "duplicate":
        education.append(copy.deepcopy(entry))
    elif change == "empty":
        education = []
    elif change == "null":
        education = None
    elif change == "not_array":
        education = entry
    else:
        entry["degree_id"] = False
    payload["job_application"]["educations"] = education
    with pytest.raises(ValueError):
        final(controller, payload)


@pytest.mark.parametrize("body", [b'{"job_application":{},"job_application":{}}',
    b'{"job_application":{"data_compliance":{},"data_compliance":{"consent":true}}}',
    b'{"job_application":{"latitude":NaN}}', b'[]', b'null', b'\xff'])
def test_malformed_duplicate_and_nonfinite_json_rejected(config, initializer, body):
    controller = ready(config, initializer)
    with pytest.raises(ValueError):
        controller.reserve_final(transport.SUBMISSION_URL, "application/json", body)


def test_durable_callback_failure_burns_permission(config, initializer):
    calls = []
    def failure(digest):
        calls.append(digest)
        raise OSError("Synthetic persistence failure")
    config["reserve_submission"] = failure
    controller = ready(config, initializer)
    payload = application(config, initializer)
    with pytest.raises(OSError):
        final(controller, payload)
    with pytest.raises(ValueError, match="already reserved"):
        final(controller, payload)
    assert len(calls) == 1


def test_concurrent_final_requests_only_reserve_once(config, initializer):
    calls = []
    config["reserve_submission"] = calls.append
    controller = ready(config, initializer)
    payload = application(config, initializer)
    def attempt(_):
        try:
            final(controller, payload)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(16)))
    assert results.count(True) == len(calls) == 1


@pytest.mark.parametrize("suffix,accepted", [("", True), ("/", True), ("//", False),
                                           ("/?x=1", False), ("/other", False)])
def test_storage_origin_browser_slash_only(config, initializer, suffix, accepted):
    controller = transport.NuroTransport(**config)
    controller.bind_initializer(INIT_URL, initializer)
    mime, body = multipart(config, initializer, "resume")
    if accepted:
        assert controller.validate_upload(transport.STORAGE_URL + suffix, mime, body)
    else:
        with pytest.raises(ValueError):
            controller.validate_upload(transport.STORAGE_URL + suffix, mime, body)


def browser_native_flow(config, initializer, renderer_source=""):
    """All routes fulfilled/aborted; dead proxy also prevents real connections.

    Optional renderer_source is for a separate private, hash-bound experiment
    with unchanged public functions. CI uses the explicit fixture below and
    does not depend on a downloaded asset, employer service, or private file.
    """
    from playwright.sync_api import sync_playwright

    calls, uploads, rejected, unexpected = [], [], [], []
    config["reserve_submission"] = calls.append
    config["approved_fields"]["phone"] = "202-555-0100"
    controller = transport.NuroTransport(**config)
    controller.bind_initializer(INIT_URL, initializer)
    origin = "https://boards.greenhouse.io"
    page_url = origin + "/local-only-transport-fixture"
    cors = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "content-type"}

    def route_request(route):
        request = route.request
        if request.method == "GET" and request.url == page_url:
            route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>Local transport test</title>")
        elif request.method == "OPTIONS" and request.url == transport.STORAGE_URL + "/":
            route.fulfill(status=204, headers=cors)
        elif request.method == "POST" and request.url == transport.STORAGE_URL + "/":
            try:
                ticket = controller.validate_upload(request.url, request.headers["content-type"], request.post_data_buffer)
                uploads.append({"url": request.url, "bytes": len(request.post_data_buffer)})
                route.fulfill(status=201, headers=cors, body="")
                controller.mark_upload_response(ticket, 201)
            except (ValueError, TypeError) as exc:
                rejected.append(type(exc).__name__ + ":" + str(exc))
                route.fulfill(status=409, headers=cors, body="fixture rejected")
        elif request.method == "POST" and request.url == transport.SUBMISSION_URL:
            try:
                proof = controller.reserve_final(request.url, request.headers["content-type"], request.post_data_buffer)
                assert proof["employer_confirmation_verified"] is False
                route.fulfill(status=200, content_type="application/json", body='{"local_fixture":true}')
            except (ValueError, TypeError) as exc:
                rejected.append(type(exc).__name__ + ":" + str(exc))
                route.fulfill(status=409, body="fixture rejected")
        else:
            unexpected.append({"method": request.method, "url": request.url})
            route.abort()

    with socket.socket() as proxy, sync_playwright() as playwright:
        proxy.bind(("127.0.0.1", 0))
        browser = playwright.chromium.launch(headless=True,
            proxy={"server": f"http://127.0.0.1:{proxy.getsockname()[1]}"},
            args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--proxy-bypass-list=<-loopback>"])
        context = browser.new_context(service_workers="block", timezone_id="America/Los_Angeles")
        context.route("**/*", route_request)
        page = context.new_page()
        page.goto(page_url)
        if renderer_source:
            page.add_script_tag(content=renderer_source + ";window.sourceRenderer={ya,Na};")
        result = page.evaluate("""async ({fields, location, docs, initializer, submit}) => {
            const uploaded = {};
            for (const kind of ['resume', 'cover_letter']) {
                const spec = initializer[kind];
                const file = new File([new Uint8Array(docs[kind].bytes)], docs[kind].name, {type:'application/pdf'});
                let form, key;
                if (window.sourceRenderer) {
                    const uploader = new window.sourceRenderer.ya({field:kind, formFields:spec.fields,
                        url:initializer.url, key:spec.key});
                    form = uploader.createForm(file);
                    key = uploader.key;
                } else {
                    form = new FormData();
                    form.append('utf8', '✓');
                    for (const name in spec.fields) form.append(name, spec.fields[name]);
                    key = spec.key.replace('{timestamp}', String(Date.now()))
                        .replace('{unique_id}', Math.random().toString(36).slice(2,16));
                    form.append('key', key);
                    form.append('authenticity_token', '1234');
                    form.append('Content-Type', 'application/octet-stream');
                    form.append('file', file);
                }
                const status = await new Promise(resolve => {
                    const xhr = new XMLHttpRequest();
                    xhr.upload.onprogress = () => {};
                    xhr.onloadend = () => resolve(xhr.status);
                    xhr.open('POST', initializer.url);
                    xhr.send(form);
                });
                if (status !== 201) return {uploadFailed:kind, status};
                uploaded[kind] = {name:file.name, url:initializer.url + '/' + key};
            }
            const values = {...fields, ...location, ...uploaded, phone:'+12025550100'};
            const questions = ['69052553','69052554','69052555','69052556','69052557'].map((id, i) => ({
                fields:[{name:'question_'+id, type:i<2?'input_text':'multi_value_single_select'}]}));
            let application;
            if (window.sourceRenderer) {
                application = window.sourceRenderer.Na({questions}, values, {}, {}, [{key:0}], [{key:0}], new URLSearchParams());
            } else {
                application = {first_name:values.first_name, last_name:values.last_name, email:values.email,
                    phone:values.phone, ...location, answers_attributes:{}, demographic_answers:[],
                    data_compliance:{}, attachments:{}, from_job_board_renderer:true,
                    employments:[{company_name:undefined,title:undefined,start_date:{month:null,year:null},
                        end_date:{month:null,year:null},current:false}],
                    educations:[{school_name_id:null,degree_id:null,discipline_id:null,
                        start_date:{month:null,year:null},end_date:{month:null,year:null}}],
                    mapped_url_token:null, appcast_click_id:null,
                    time_zone:Intl.DateTimeFormat().resolvedOptions().timeZone};
                questions.forEach((q, priority) => {
                    const name = q.fields[0].name, id = name.split('_')[1];
                    application.answers_attributes[id] = priority<2
                        ? {question_id:id, priority, text_value:values[name]}
                        : {question_id:id, priority, boolean_value:+values[name]};
                });
                for (const kind of ['resume','cover_letter']) {
                    application[kind+'_url'] = uploaded[kind].url;
                    application[kind+'_url_filename'] = uploaded[kind].name;
                }
            }
            const body = JSON.stringify({job_application:application, fingerprint:'synthetic-opaque-fingerprint',
                'g-recaptcha-enterprise-token':'synthetic-opaque-proof'});
            const statuses = [];
            for (let i=0;i<2;i++) statuses.push((await fetch(submit, {
                method:'POST', headers:{'Content-Type':'application/json'}, body})).status);
            return {statuses, educationPlaceholder:application.educations};
        }""", {"fields": config["approved_fields"], "location": config["location"],
                "docs": {kind: {"name": doc["name"], "bytes": list(doc["content"])}
                         for kind, doc in config["documents"].items()},
                "initializer": initializer, "submit": transport.SUBMISSION_URL})
        browser.close()
    assert result == {"statuses": [200, 409], "educationPlaceholder": [transport.EMPTY_EDUCATION]}, (result, rejected)
    assert len(uploads) == 2 and all(item["url"] == transport.STORAGE_URL + "/" for item in uploads)
    assert len(calls) == 1
    assert len(rejected) == 1 and "already reserved" in rejected[0]
    assert unexpected == []
    return {"uploads_validated": 2, "final_reservations": len(calls), "replay_rejected": True,
            "native_trailing_slash_observed": True, "unexpected_requests": unexpected,
            "employer_requests_forwarded": 0, "source_renderer_used": bool(renderer_source),
            "actual_initial_employment_and_education_placeholders": True}


@pytest.mark.browser
def test_native_chromium_formdata_and_final_json_with_all_network_denied(config, initializer):
    browser_native_flow(config, initializer)
