"""Conservative identities: remove tracking without dropping requisition IDs."""

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_KEYS = {"gclid", "fbclid"}


def normalize_url(value: str) -> str:
    """Validate HTTP URLs and preserve all potentially job-identifying parameters."""
    parsed = urlsplit(str(value).strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Job URL must be absolute HTTP(S)")
    if parsed.username or parsed.password:
        raise ValueError("Credentials must not appear in job URLs")
    host = parsed.hostname.lower().encode("idna").decode("ascii")
    port = parsed.port
    if ":" in host:
        host = f"[{host}]"
    if port and (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_KEYS]
    return urlunsplit((parsed.scheme.lower(), host, parsed.path.rstrip("/") or "/",
                       urlencode(sorted(query)), ""))


def normalized_text(value: str | None) -> str:
    """Normalize punctuation and case without fuzzy inference."""
    return re.sub(r"[^\w]+", " ", value or "").strip().casefold()


def job_fingerprint(job: dict) -> str | None:
    """Use a reviewed employer requisition, otherwise the conservative tuple.

    ``verified_requisition_id`` is an explicit importer assertion backed by an
    official posting. It prevents distinct teams' same-title jobs being collapsed.
    Location variants of the same employer requisition still share one identity.
    Do not populate it from unverified board IDs or a generated identifier.
    """
    company = normalized_text(job.get("company"))
    requisition = job.get("verified_requisition_id")
    if company and isinstance(requisition, str) and requisition.strip():
        return hashlib.sha256(f"requisition|{company}|{requisition.strip().casefold()}".encode()).hexdigest()
    parts = [normalized_text(job.get(key)) for key in ("company", "title", "location")]
    if not all(parts):
        return None
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
