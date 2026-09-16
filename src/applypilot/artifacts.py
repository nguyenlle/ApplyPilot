"""Bind generated documents to one posting and detect stale or changed uploads."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path


def make_filename_prefix(job: dict) -> str:
    """Stable URL identity prevents same-title postings overwriting each other."""
    url = str(job.get("url") or "").strip()
    if not url:
        raise ValueError("A job URL is required for document generation")
    title = re.sub(r"[^\w-]+", "_", str(job.get("title") or "job"))[:50].strip("_")
    company = re.sub(r"[^\w-]+", "_", str(job.get("company") or job.get("site") or "job"))[:20].strip("_")
    return f"{company}_{title}_{hashlib.sha256(url.encode()).hexdigest()[:16]}"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_artifact(text_path: Path) -> None:
    """Invalidate old derived files before replacing their source document."""
    text_path.with_suffix(".manifest.json").unlink(missing_ok=True)
    text_path.with_suffix(".pdf").unlink(missing_ok=True)


def write_manifest(job: dict, text_path: Path, *, kind: str, approved: bool,
                   local_receipt: dict | None = None) -> Path:
    """Write only after text/PDF generation. Missing PDFs cannot be uploaded."""
    text_path = Path(text_path)
    pdf_path = text_path.with_suffix(".pdf")
    manifest = {
        "version": 1, "job_url": job["url"], "title": job.get("title"),
        "company": job.get("company") or job.get("site"), "kind": kind,
        "approved": approved, "text_sha256": _digest(text_path),
        "pdf_sha256": _digest(pdf_path) if pdf_path.is_file() else None,
    }
    if local_receipt is not None:
        manifest["local_preparation"] = local_receipt
    path = text_path.with_suffix(".manifest.json")
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def _verify_local_receipt(job: dict, text_path: Path, manifest: dict) -> None:
    """Only a committed preparation receipt can authorize local-task artifacts.

    A fresh read-only connection deliberately cannot see an import transaction's
    uncommitted receipt, including after a process crash leaves an orphan file.
    """
    from applypilot import config

    root = (config.APP_DIR / "local-handoffs").absolute()
    local = text_path.absolute().is_relative_to(root)
    if not local and "local_preparation" not in manifest:
        return
    from applypilot.local_handoff import job_content_digest, object_digest, private_path

    text_path = private_path(text_path)
    binding = manifest.get("local_preparation")
    if (not local or text_path.parent.parent != root or not isinstance(binding, dict)
            or set(binding) != {"handoff_id", "receipt_sha256"}
            or binding["handoff_id"] != text_path.parent.name):
        raise ValueError("Local artifact lacks its pinned preparation receipt")
    expected_name = {"resume": "resume-tailored.txt", "cover_letter": "cover-letter.txt"}.get(manifest.get("kind"))
    if text_path.name != expected_name:
        raise ValueError("Local artifact filename differs from its reviewed path")
    try:
        with closing(sqlite3.connect(private_path(config.DB_PATH).as_uri() + "?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT job_url,imported_at,receipt_json FROM local_preparation_handoffs WHERE handoff_id=?",
                               (binding["handoff_id"],)).fetchone()
        if not row or not row[1] or not row[2]:
            raise ValueError("Local preparation has no committed import receipt")
        receipt = json.loads(row[2])
        if not isinstance(receipt, dict) or not isinstance(receipt.get("files"), dict):
            raise TypeError("Malformed committed local preparation receipt")
        if (row[0] != job.get("url") or receipt.get("job_url") != job.get("url")
                or object_digest(receipt) != binding["receipt_sha256"]
                or receipt.get("job_content_sha256") != job_content_digest(job)
                or receipt.get("files", {}).get(text_path.name) != manifest.get("text_sha256")
                or receipt.get("files", {}).get(text_path.with_suffix(".pdf").name) != manifest.get("pdf_sha256")):
            raise ValueError("Local artifact differs from its committed review receipt")
    except (sqlite3.Error, OSError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Cannot verify committed local preparation receipt") from exc


def verify_artifact(job: dict, text_path: str | Path, *, kind: str) -> Path:
    """Return verified PDF path or raise ValueError before an upload occurs."""
    text_path = Path(text_path)
    pdf_path = text_path.with_suffix(".pdf")
    try:
        manifest = json.loads(text_path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise TypeError("Document manifest must be an object")
        if manifest.get("job_url") != job.get("url") or manifest.get("kind") != kind:
            raise ValueError("Document belongs to another job or document type")
        if manifest.get("approved") is not True:
            raise ValueError("Document has not passed validation")
        _verify_local_receipt(job, text_path, manifest)
        if manifest.get("text_sha256") != _digest(text_path):
            raise ValueError("Document text changed after validation")
        if not manifest.get("pdf_sha256") or manifest["pdf_sha256"] != _digest(pdf_path):
            raise ValueError("Document PDF is missing, stale, or changed")
        if not pdf_path.read_bytes().startswith(b"%PDF-"):
            raise ValueError("Document is not a PDF")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot verify job document: {text_path.name}") from exc
    return pdf_path


def verify_job_artifacts(job: dict) -> dict[str, Path]:
    """Verify the required resume and optional configured cover letter."""
    if not job.get("tailored_resume_path"):
        raise ValueError("Job has no tailored resume")
    verified = {"resume": verify_artifact(job, job["tailored_resume_path"], kind="resume")}
    if job.get("cover_letter_path"):
        verified["cover_letter"] = verify_artifact(job, job["cover_letter_path"], kind="cover_letter")
    return verified


def refresh_pdf_manifest(text_path: Path) -> None:
    """Record a regenerated PDF only if its source is still approved and unchanged."""
    path = text_path.with_suffix(".manifest.json")
    if not path.exists():
        return  # Initial generation writes the full manifest afterward.
    manifest = json.loads(path.read_text(encoding="utf-8"))
    from applypilot import config
    if "local_preparation" in manifest or text_path.absolute().is_relative_to((config.APP_DIR / "local-handoffs").absolute()):
        raise ValueError("Local PDF regeneration requires a fresh handoff and independent review")
    if not manifest.get("approved") or manifest.get("text_sha256") != _digest(text_path):
        raise ValueError("Cannot certify a PDF generated from unapproved or changed text")
    manifest["pdf_sha256"] = _digest(text_path.with_suffix(".pdf"))
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
