"""Pure, single-attempt validation of the reviewed Nuro Greenhouse wire format.

No requests are sent here. The caller owns a fresh isolated browser, trusted
response provenance, submission arming, durable reservation, and confirmation.
Passing these checks does not qualify an employer receipt or authorize a retry.
Security envelope values and presigned fields are kept opaque and never logged.
"""

import hashlib
import json
import math
import re
import threading
import uuid
from collections.abc import Callable
from email.parser import BytesParser
from email.policy import default
from urllib.parse import parse_qs, urlsplit

from applypilot.apply.nuro import FIELDS, JOB_URL, POSTING_ID

STORAGE_URL = "https://grnhse-prod-jben-us-west-2.s3.us-west-2.amazonaws.com"
SUBMISSION_URL = "https://boards.greenhouse.io/embed/nuro/jobs/8187498"
INITIALIZER_URL = "https://boards.greenhouse.io/uncacheable_attributes/presigned_fields"
CONFIRMATION_URL = "https://job-boards.greenhouse.io/embed/job_app/confirmation?for=nuro&token=8187498"
SIGNED_FIELDS = frozenset({"x-amz-server-side-encryption", "success_action_status", "policy",
                          "x-amz-credential", "x-amz-algorithm", "x-amz-date", "x-amz-signature"})
SECURITY_KEYS = frozenset({"fingerprint", "g-recaptcha-enterprise-token", "request_token", "authenticity_token"})
EMPTY_EMPLOYMENT = {"start_date": {"month": None, "year": None},
                    "end_date": {"month": None, "year": None}, "current": False}
EMPTY_EDUCATION = {"school_name_id": None, "degree_id": None, "discipline_id": None,
                   "start_date": {"month": None, "year": None}, "end_date": {"month": None, "year": None}}


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _strict_json(body: bytes) -> dict:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("Duplicate JSON key")
            out[key] = value
        return out
    def invalid(_):
        raise ValueError("Non-finite JSON number")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Malformed application JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("Application JSON must be an object")
    return value


class NuroTransport:
    """Validate one approved pair of uploads and reserve at most one final POST.

    approved_fields uses names/values from nuro.prepare_fields()['fields'].
    documents is {kind: {name, sha256, content: bytes}} for both approved PDFs.
    location is the exact observed selected location/latitude/longitude/
    country_short_name strings, not inferred coordinates. reserve_submission
    must durably consume the parent attempt before returning. Its failure burns
    this controller's permission too; do not reconstruct it to retry.
    """

    def __init__(self, *, approved_fields: dict, documents: dict, location: dict,
                 reviewed_storage_url: str, time_zone: str,
                 reserve_submission: Callable[[str], None]):
        if reviewed_storage_url != STORAGE_URL:
            raise ValueError("Unreviewed storage destination")
        if not isinstance(approved_fields, dict) or set(approved_fields) - set(FIELDS):
            raise ValueError("Unapproved applicant field")
        required = {name for name, (_, _, needed, _) in FIELDS.items() if needed}
        if not required <= set(approved_fields):
            raise ValueError("Missing approved applicant answer")
        if any(not isinstance(v, str) or not v.strip() or len(v) > 8192 for v in approved_fields.values()):
            raise ValueError("Approved answers must be nonempty strings")
        for name in ("question_69052555", "question_69052556", "question_69052557"):
            if approved_fields[name] not in {"0", "1"}:
                raise ValueError("Approved legal answers must be exact 0/1 strings")
        if (not isinstance(location, dict)
                or set(location) != {"location", "latitude", "longitude", "country_short_name"}
                or any(not isinstance(v, str) or not v.strip() or len(v) > 1024 for v in location.values())
                or location.get("country_short_name") != "US"):
            raise ValueError("Exact observed US location values required")
        try:
            lat, lon = float(location["latitude"]), float(location["longitude"])
            if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError("Invalid observed coordinates")
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid observed coordinates") from exc
        if not isinstance(time_zone, str) or not re.fullmatch(r"[A-Za-z_]+/[A-Za-z_+-]+(?:/[A-Za-z_+-]+)?", time_zone):
            raise ValueError("Explicit observed browser time zone required")
        if not callable(reserve_submission):
            raise TypeError("Durable reservation callback required")
        if not isinstance(documents, dict) or set(documents) != {"resume", "cover_letter"}:
            raise ValueError("Both exact approved PDFs are required")
        self._documents = {}
        for kind, doc in documents.items():
            if (not isinstance(doc, dict) or set(doc) != {"name", "sha256", "content"}
                    or not isinstance(doc["name"], str) or not doc["name"].endswith(".pdf")
                    or any(c in doc["name"] for c in '/\\\r\n\x00')
                    or not 1 <= len(doc["name"]) <= 255
                    or type(doc["content"]) is not bytes or not doc["content"].startswith(b"%PDF-")
                    or len(doc["content"]) > 10_000_000
                    or hashlib.sha256(doc["content"]).hexdigest() != doc["sha256"]):
                raise ValueError("Invalid approved PDF or digest")
            self._documents[kind] = dict(doc)
        self._approved = dict(approved_fields)
        self._location = dict(location)
        self._time_zone = time_zone
        self._reserve_submission = reserve_submission
        self._initializers: dict = {}
        self._tickets: dict = {}
        self._upload_kinds: set = set()
        self._uploaded: dict = {}
        self._final_spent = False
        self._lock = threading.Lock()

    def bind_initializer(self, request_url: str, response: dict, *, status: int = 200) -> None:
        """Bind a trusted actual initializer response supplied by the parent.

        Origin/status arguments cannot themselves prove network provenance. The
        caller must capture this response from its intercepted exact GET, without
        redirects, and must never log the returned signing material.
        """
        if not isinstance(request_url, str):
            raise TypeError("Unreviewed initializer URL")
        parts = urlsplit(request_url)
        try:
            query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise ValueError("Malformed initializer query") from exc
        if (f"{parts.scheme}://{parts.netloc}{parts.path}" != INITIALIZER_URL or parts.fragment
                or query != {"fields[]": ["resume", "cover_letter"]}
                or type(status) is not int or status != 200):
            raise ValueError("Unreviewed initializer request or response status")
        if not isinstance(response, dict) or set(response) != {"url", "resume", "cover_letter"}:
            raise ValueError("Unexpected initializer response shape")
        if response["url"] != STORAGE_URL:
            raise ValueError("Initializer returned unreviewed storage destination")
        bound = {}
        for kind in ("resume", "cover_letter"):
            data = response[kind]
            if (not isinstance(data, dict) or set(data) != {"fields", "key"}
                    or not isinstance(data["fields"], dict) or set(data["fields"]) != SIGNED_FIELDS
                    or any(not isinstance(v, str) or not v or len(v) > 32768 for v in data["fields"].values())
                    or not isinstance(data["key"], str)
                    or not re.fullmatch(r"stash/applications/resumes/\{timestamp\}-\{unique_id\}-[a-f0-9]{32}", data["key"])):
                raise ValueError("Unreviewed presigned upload fields or key template")
            pattern = re.escape(data["key"]).replace(re.escape("{timestamp}"), r"[0-9]{13}")
            pattern = pattern.replace(re.escape("{unique_id}"), r"[a-z0-9]{1,14}")
            bound[kind] = {"fields": dict(data["fields"]), "pattern": pattern}
        if bound["resume"]["pattern"] == bound["cover_letter"]["pattern"]:
            raise ValueError("Upload key templates must identify distinct objects")
        with self._lock:
            if self._initializers or self._tickets or self._final_spent:
                raise ValueError("Initializer already bound; rearming is forbidden")
            self._initializers = bound

    def validate_upload(self, url: str, content_type: str, body: bytes) -> str:
        """Reserve one upload and return an opaque ticket before caller forwards."""
        if url not in {STORAGE_URL, STORAGE_URL + "/"} or not isinstance(content_type, str) or not content_type.lower().startswith("multipart/form-data;"):
            raise ValueError("Unreviewed upload destination or encoding")
        if type(body) is not bytes or len(body) > 11_000_000 or '\r' in content_type or '\n' in content_type:
            raise ValueError("Invalid upload body")
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body)
        if (not message.is_multipart() or message.defects
                or any(header.defects for header in message.values())
                or message.preamble or message.epilogue
                or {name for name, _ in message.get_params()[1:]} != {"boundary"}):
            raise ValueError("Malformed upload multipart")
        scalars, filename, content = {}, None, None
        for part in message.iter_parts():
            if (part.is_multipart() or part.defects or any(header.defects for header in part.values())
                    or part.get("Content-Transfer-Encoding") is not None
                    or {name.lower() for name in part} - {"content-disposition", "content-type"}
                    or len(part.get_all("Content-Disposition", [])) != 1
                    or len(part.get_all("Content-Type", [])) > 1):
                raise ValueError("Nested or malformed upload part")
            name = part.get_param("name", header="content-disposition")
            if not name or part.get_content_disposition() != "form-data":
                raise ValueError("Unnamed upload part")
            data = part.get_payload(decode=True)
            if data is None:
                raise ValueError("Invalid upload part bytes")
            if part.get_filename() is not None:
                if (name != "file" or content is not None or "file" in scalars
                        or part.get_content_type() != "application/pdf"
                        or {key for key, _ in part.get_params(header="content-disposition")[1:]} != {"name", "filename"}
                        or len(part.get_params()) != 1):
                    raise ValueError("Duplicate or unapproved upload file")
                filename, content = part.get_filename(), data
            else:
                if (name in scalars or name == "file" or part.get("Content-Type") is not None
                        or {key for key, _ in part.get_params(header="content-disposition")[1:]} != {"name"}):
                    raise ValueError("Duplicate or unapproved upload field")
                try:
                    scalars[name] = data.decode("utf-8", errors="strict")
                except UnicodeError as exc:
                    raise ValueError("Invalid upload field encoding") from exc
        with self._lock:
            if not self._initializers or self._final_spent:
                raise ValueError("Upload initializer unavailable or attempt closed")
            matches = []
            for kind, spec in self._initializers.items():
                expected = {**spec["fields"], "utf8": "✓", "authenticity_token": "1234",
                            "Content-Type": "application/octet-stream", "key": scalars.get("key")}
                doc = self._documents[kind]
                if (scalars == expected and isinstance(scalars.get("key"), str)
                        and re.fullmatch(spec["pattern"], scalars["key"])
                        and filename == doc["name"] and content == doc["content"]):
                    matches.append(kind)
            if len(matches) != 1:
                raise ValueError("Upload does not match one approved file and exact server fields")
            kind = matches[0]
            if kind in self._upload_kinds:
                raise ValueError("Upload already reserved; automatic retries forbidden")
            reference = STORAGE_URL + "/" + scalars["key"]
            if any(ticket["url"] == reference for ticket in self._tickets.values()):
                raise ValueError("Upload object URL already reserved for another document")
            ticket = uuid.uuid4().hex
            self._upload_kinds.add(kind)
            self._tickets[ticket] = {"kind": kind, "url": reference, "finished": False}
            return ticket

    def mark_upload_response(self, ticket: str, status: int) -> None:
        """Bind a reference only after the caller observes this upload's 2xx.

        A failure consumes the upload permission and never creates a reference.
        The caller must not mark an unrelated, redirected or fabricated response.
        """
        with self._lock:
            if not isinstance(ticket, str):
                raise TypeError("Invalid upload ticket")
            attempt = self._tickets.get(ticket)
            if not attempt or attempt["finished"] or type(status) is not int or not 100 <= status <= 599:
                raise ValueError("Unknown or already completed upload ticket")
            attempt["finished"] = True
            if 200 <= status < 300:
                self._uploaded[attempt["kind"]] = attempt["url"]

    def _expected_application(self) -> dict:
        if set(self._uploaded) != {"resume", "cover_letter"}:
            raise ValueError("Both exact uploads must succeed before submission")
        result = {name: self._approved[name] for name in ("first_name", "last_name", "email", "phone")}
        result.update(self._location)
        result.update(answers_attributes={}, demographic_answers=[], data_compliance={}, attachments={},
                      from_job_board_renderer=True, employments=[], mapped_url_token=None,
                      appcast_click_id=None, time_zone=self._time_zone)
        for priority, number in enumerate(("69052553", "69052554", "69052555", "69052556", "69052557")):
            name = "question_" + number
            answer = {"question_id": number, "priority": priority}
            if priority < 2:
                if name in self._approved:
                    answer["text_value"] = self._approved[name]
            else:
                answer["boolean_value"] = int(self._approved[name])
            result["answers_attributes"][number] = answer
        for kind, reference in self._uploaded.items():
            result[kind + "_url"] = reference
            result[kind + "_url_filename"] = self._documents[kind]["name"]
        return result

    def reserve_final(self, url: str, content_type: str, body: bytes) -> dict:
        """Validate and durably reserve the ONLY final POST before forwarding.

        The returned hash is safe evidence; it omits opaque browser security
        values. This method sends nothing and makes no claim about acceptance.
        """
        if (url != SUBMISSION_URL or not isinstance(content_type, str)
                or content_type.split(";", 1)[0].strip().lower() != "application/json"):
            raise ValueError("Wrong Nuro job submission destination or encoding")
        if type(body) is not bytes or len(body) > 100_000:
            raise ValueError("Invalid application body")
        try:
            payload = _strict_json(body)
        except TypeError as exc:
            raise ValueError("Malformed application JSON object") from exc
        if "job_application" not in payload or set(payload) - {"job_application", *SECURITY_KEYS}:
            raise ValueError("Unreviewed security envelope or top-level field")
        if any(not isinstance(v, str) or not v or len(v) > 32768
               for k, v in payload.items() if k != "job_application"):
            raise ValueError("Unreviewed security envelope shape")
        application = payload["job_application"]
        if not isinstance(application, dict):
            raise TypeError("Malformed application facts")
        with self._lock:
            if self._final_spent:
                raise ValueError("Final submission already reserved; all retries forbidden")
            expected = self._expected_application()
            # Renderer may include empty employment/education placeholders even
            # when those controls are hidden. It may also explicitly serialize blank
            # optional text. Neither variant is permission to send new facts.
            checked = dict(application)
            # The US phone widget serializes getNumber() in E.164 form. Only
            # punctuation and an optional US country prefix may differ from the
            # approved number; extensions or other countries are not inferred.
            approved_phone = self._approved["phone"]
            if re.fullmatch(r"[+0-9(). -]+", approved_phone):
                digits = re.sub(r"[^0-9]", "", approved_phone)
                normalized = "+1" + digits if len(digits) == 10 else "+" + digits
                if re.fullmatch(r"\+1[0-9]{10}", normalized) and checked.get("phone") == normalized:
                    checked["phone"] = approved_phone
            if _canonical(checked.get("employments")) == _canonical([EMPTY_EMPLOYMENT]):
                checked["employments"] = []
            if _canonical(checked.get("educations")) == _canonical([EMPTY_EDUCATION]):
                del checked["educations"]
            answers = checked.get("answers_attributes")
            if isinstance(answers, dict):
                answers = {key: dict(value) if isinstance(value, dict) else value for key, value in answers.items()}
                for number in ("69052553", "69052554"):
                    answer = answers.get(number)
                    if "question_" + number not in self._approved and isinstance(answer, dict) and answer.get("text_value") == "":
                        del answer["text_value"]
                checked["answers_attributes"] = answers
            if _canonical(checked) != _canonical(expected):
                raise ValueError("Application facts, consents, job references or upload references differ from approval")
            digest = hashlib.sha256(body).hexdigest()
            self._final_spent = True  # Burn permission before potentially failing durable callback.
            self._reserve_submission(digest)
            return {"job_url": JOB_URL, "posting_id": POSTING_ID, "request_sha256": digest,
                    "request_validated": True, "submission_reserved": True, "employer_confirmation_verified": False}
