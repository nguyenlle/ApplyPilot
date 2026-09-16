"""Fail-closed submission gate for explicitly configured native HTML forms.

This is NOT a generic ATS safety guarantee. Supported pages cannot require site
JavaScript: the gate strips that capability through CSP. Every URL, scalar
field, consent and uploaded byte must match a reviewed per-job descriptor.
Workday/Greenhouse/other dynamic adapters require separate integration work.
"""

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from applypilot import config
from applypilot.artifacts import verify_job_artifacts
from applypilot.identity import normalized_text

logger = logging.getLogger(__name__)


class UnsupportedAdapter(ValueError):
    """No reviewed, deployable adapter matches this specific queued job."""


@dataclass(frozen=True)
class PreparedAdapter:
    application_url: str
    submission_url: str
    confirmation_urls: tuple[str, ...]
    fields: dict[str, str]
    files: dict[str, str]
    company: str
    title: str


def _safe_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise UnsupportedAdapter("Adapter URLs must be exact HTTP(S) URLs without credentials or fragments")
    return value


def prepare_adapter(job: dict, profile: dict | None = None, *, adapter_path: Path | None = None) -> PreparedAdapter:
    """Validate adapter/facts/artifacts before launching the application agent.

    application_adapters.json is {"adapters": [{"kind":"native_html_v1",
    "job_url":..., "application_url":..., "submission_url":...,
    "confirmation_urls":[...], "company":..., "title":...,
    "job_id_field":"job_id", "job_id":"123", "fields":{"email":"personal.email"},
    "consents":{"terms":"legal.example_terms"}, "files":{"resume":"resume"}}]}.
    All configured fields are required; extra fields and duplicate keys are denied.
    Consent values must be explicitly configured strings matching the form payload.
    """
    profile = config.load_profile() if profile is None else profile
    path = adapter_path or config.APP_DIR / "application_adapters.json"
    if not path.is_file():
        raise UnsupportedAdapter("No reviewed native-form adapter configured; autonomous submission disabled")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        entries = [item for item in document["adapters"] if item.get("job_url") == job["url"]]
        if len(entries) != 1:
            raise UnsupportedAdapter("Exactly one reviewed adapter must match the queued job URL")
        entry = entries[0]
        if entry.get("kind") != "native_html_v1":
            raise UnsupportedAdapter("Only the tested native_html_v1 adapter is supported")
        company, title = job.get("company"), job.get("title")
        if not company or not title or entry["company"] != company or entry["title"] != title:
            raise UnsupportedAdapter("Adapter employer/title differs from the queued job")
        application = _safe_url(entry["application_url"])
        if application != (job.get("application_url") or job["url"]):
            raise UnsupportedAdapter("Adapter application URL differs from the queued job")
        submission = _safe_url(entry["submission_url"])
        confirmations = tuple(_safe_url(url) for url in entry.get("confirmation_urls", []))
        # A native form adapter cannot silently send documents to another host.
        expected_origin = urlsplit(application)[:2]
        if any(urlsplit(url)[:2] != expected_origin for url in (submission, *confirmations)):
            raise UnsupportedAdapter("Cross-origin submission/confirmation is unsupported")
        fields = {}
        for group in ("fields", "consents"):
            for name, fact_path in entry.get(group, {}).items():
                value = profile
                for part in fact_path.split("."):
                    value = value[part]
                if value is None or value == "" or isinstance(value, (dict, list, bool)):
                    raise UnsupportedAdapter(f"Required factual field {name} needs an explicit scalar answer")
                if any(word in fact_path.lower() for word in ("password", "secret", "token", "api_key")):
                    raise UnsupportedAdapter("Credentials must not be configured as application fields")
                if name in fields:
                    raise UnsupportedAdapter("Adapter has overlapping field/consent names")
                fields[name] = str(value)
        identity_field, identity = entry["job_id_field"], entry["job_id"]
        if not isinstance(identity, str) or not identity or identity_field in fields:
            raise UnsupportedAdapter("Adapter requires a distinct explicit job identity field")
        fields[identity_field] = identity
        verified = verify_job_artifacts(job)
        files = {}
        for name, kind in entry.get("files", {}).items():
            if name in fields or kind not in verified:
                raise UnsupportedAdapter("Unknown document type or overlapping form field")
            files[name] = hashlib.sha256(verified[kind].read_bytes()).hexdigest()
        if "resume" not in entry.get("files", {}).values():
            raise UnsupportedAdapter("Native form adapter must bind the verified resume upload")
        return PreparedAdapter(application, submission, confirmations, fields, files, company, title)
    except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise UnsupportedAdapter("Malformed adapter or missing required profile fact") from exc


def validate_payload(content_type: str, body: bytes, adapter: PreparedAdapter) -> None:
    """Reject unexpected fields, invented answers, wrong job IDs or changed files."""
    scalars = {}
    files = {}
    if content_type.lower().startswith("multipart/form-data"):
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
        if not message.is_multipart():
            raise ValueError("Malformed multipart payload")
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name or name in scalars or name in files:
                raise ValueError("Missing or duplicate form field")
            value = part.get_payload(decode=True)
            if value is None:
                raise ValueError("Malformed field bytes")
            if part.get_filename() is not None:
                files[name] = hashlib.sha256(value).hexdigest()
            else:
                scalars[name] = value.decode("utf-8", errors="strict")
    elif content_type.lower().startswith("application/x-www-form-urlencoded"):
        parsed = parse_qs(body.decode("utf-8", errors="strict"), keep_blank_values=True, strict_parsing=True)
        if any(len(values) != 1 for values in parsed.values()):
            raise ValueError("Duplicate form field")
        scalars = {name: values[0] for name, values in parsed.items()}
    else:
        raise ValueError("Unsupported submission encoding")
    if scalars != adapter.fields:
        raise ValueError("Submitted fields do not exactly match configured factual answers/job ID/consents")
    if files != adapter.files:
        raise ValueError("Submitted file bytes do not exactly match the job's verified artifacts")


class SubmissionGate:
    """Background CDP gate stays responsive while Claude's subprocess runs."""

    def __init__(self, port: int, job: dict, adapter: PreparedAdapter, *, evidence_path: Path | None = None):
        self.port, self.job, self.adapter = port, job, adapter
        self.evidence_path = evidence_path
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._closing = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="application-network-gate", daemon=True)
        self._lock = threading.Lock()
        self._error = None
        self._submission_snapshot = None
        self._evidence = {"adapter": "native_html_v1", "attempt_id": job.get("claim_token"),
                          "job_url": job["url"], "submission_attempted": False,
                          "request_validated": False, "response_received": False, "blocked": []}

    def __enter__(self):
        if self.evidence_path:
            self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread.start()
        if not self._ready.wait(20):
            self._stop.set()
            from applypilot.apply.chrome import abort_owned_chrome
            abort_owned_chrome(self.port)
            raise RuntimeError("Application network gate did not initialize")
        if self._error:
            raise RuntimeError(f"Application network gate failed: {self._error}")
        return self

    def __exit__(self, *_):
        # Detaching CDP may resume pending requests. Keep interception alive
        # until the owned browser has flushed its session and actually closed.
        from applypilot.apply.chrome import close_owned_chrome
        self._closing.set()
        close_owned_chrome(self.port)
        self._stop.set()
        self._thread.join(timeout=15)
        if self._thread.is_alive():
            from applypilot.apply.chrome import abort_owned_chrome
            abort_owned_chrome(self.port)
            raise RuntimeError("Application network gate did not shut down")

    def evidence(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._evidence))

    def _record(self, **values):
        with self._lock:
            self._evidence.update(values)
            self._persist()

    def _persist(self):
        if self.evidence_path:
            temporary = self.evidence_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._evidence, indent=2), encoding="utf-8")
            temporary.replace(self.evidence_path)

    def _consume_submission(self, url: str, body: bytes):
        """Atomic compare-and-set: callbacks cannot race a second POST through."""
        with self._lock:
            if self._evidence["submission_attempted"]:
                raise ValueError("Submission token already consumed; automatic retries forbidden")
            self._evidence.update(submission_attempted=True, request_validated=True,
                                  submission_url=url, request_sha256=hashlib.sha256(body).hexdigest(),
                                  submitted_at=time.time())
            self._persist()

    def _serve(self):
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{self.port}", timeout=10000)
                contexts = browser.contexts
                if len(contexts) != 1:
                    raise RuntimeError(f"Gate requires one dedicated context; found {len(contexts)}")
                active_workers = [worker.url for worker in contexts[0].service_workers]
                if active_workers:
                    raise RuntimeError(f"Gate refuses active site service workers: {active_workers}")
                context = contexts[0]
                context.route("**/*", self._route)
                context.route_web_socket("**/*", lambda ws: ws.close())
                page = context.new_page()
                for old in list(context.pages):
                    if old != page:
                        old.close()
                # Native form pages must never be served by old persisted workers.
                session = context.new_cdp_session(page)
                self._page, self._session = page, session
                binding = "__applypilot_submit_" + uuid.uuid4().hex
                session.send("Runtime.enable")
                session.send("Runtime.addBinding", {"name": binding})

                def capture(event):
                    if event.get("name") == binding:
                        self._submission_snapshot = json.loads(event["payload"])

                session.on("Runtime.bindingCalled", capture)
                # Capture the source DOM synchronously BEFORE native navigation.
                # Reading a locator while its POST is paused deadlocks navigation.
                context.add_init_script("""window.addEventListener('submit', event => {
                    const form = event.target;
                    window[""" + json.dumps(binding) + """](JSON.stringify({
                        url: location.href, action: form.action, valid: form.checkValidity(),
                        visible_text: document.body ? document.body.innerText : ''
                    }));
                }, true);""")
                origin = f"{urlsplit(self.adapter.application_url).scheme}://{urlsplit(self.adapter.application_url).netloc}"
                session.send("Storage.clearDataForOrigin", {"origin": origin, "storageTypes": "service_workers"})
                session.send("Network.setBypassServiceWorker", {"bypass": True})
                self._ready.set()
                while not self._stop.is_set():
                    page.wait_for_timeout(50)
                # Leave routing installed until this connection disconnects. Root
                # must stop the agent BEFORE exiting the gate, then close Chrome.
        except Exception as exc:
            if self._closing.is_set():
                self._stop.set()
                return
            logger.exception("Native application gate failed closed")
            from applypilot.apply.chrome import abort_owned_chrome
            abort_owned_chrome(self.port)
            self._error = str(exc)
            self._record(gate_error=self._error)
            self._ready.set()
            self._stop.set()

    def _route(self, route):
        request = route.request
        adapter = self.adapter
        url, method = request.url, request.method
        allowed_gets = {adapter.application_url, *adapter.confirmation_urls}
        try:
            if method == "GET" and url in allowed_gets:
                if url != adapter.application_url and url != self.evidence().get("redirect_confirmation_url"):
                    raise ValueError("Confirmation GET requires an actual allowlisted submission redirect")
                response = route.fetch(max_redirects=0, timeout=10000)
                if 300 <= response.status < 400:
                    raise ValueError("Unreviewed redirect blocked")
                if url == self.evidence().get("redirect_confirmation_url"):
                    self._record(confirmation_response_status=response.status)
                    if 200 <= response.status < 300:
                        self._record(confirmation_url=url)
                headers = dict(response.headers)
                headers["content-security-policy"] = (
                    "default-src 'none'; script-src 'none'; worker-src 'none'; frame-src 'none'; "
                    "object-src 'none'; connect-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                    f"form-action {adapter.submission_url}; base-uri 'none'")
                route.fulfill(response=response, headers=headers)
                return
            if method == "POST" and url == adapter.submission_url:
                if self.evidence()["submission_attempted"]:
                    raise ValueError("Submission token already consumed; automatic retries forbidden")
                frame = request.frame
                if (frame.url != adapter.application_url or frame != frame.page.main_frame
                        or frame.page != self._page):
                    raise ValueError("Submission must originate from the exact reviewed application page")
                observed, self._submission_snapshot = self._submission_snapshot, None
                if (not observed or observed.get("url") != adapter.application_url
                        or observed.get("action") != adapter.submission_url or not observed.get("valid")):
                    raise ValueError("Missing fresh native submit event from the reviewed, valid form")
                visible = normalized_text(observed.get("visible_text", ""))
                if normalized_text(adapter.company) not in visible or normalized_text(adapter.title) not in visible:
                    raise ValueError("Visible employer/title does not match the queued job")
                body = request.post_data_buffer or b""
                validate_payload(request.headers.get("content-type", ""), body, adapter)
                # Persist the attempt before sending: a lost response is uncertain.
                self._consume_submission(url, body)
                response = route.fetch(max_redirects=0, timeout=10000)
                self._record(response_received=True, response_status=response.status)
                headers = dict(response.headers)
                headers["content-security-policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'none'"
                if 300 <= response.status < 400:
                    location = response.headers.get("location", "")
                    from urllib.parse import urljoin
                    if urljoin(url, location) not in adapter.confirmation_urls:
                        raise ValueError("Submission response redirect needs review")
                    self._record(redirect_confirmation_url=urljoin(url, location))
                elif 200 <= response.status < 300:
                    self._record(confirmation_url=url, confirmation_response_status=response.status)
                route.fulfill(response=response, headers=headers)
                return
            raise ValueError("Request is outside the reviewed native-form adapter")
        except (ValueError, UnicodeError, PlaywrightError) as exc:
            with self._lock:
                self._evidence["blocked"].append({"method": method, "url": url.split("?")[0], "reason": str(exc)})
                self._persist()
            route.abort("blockedbyclient")
