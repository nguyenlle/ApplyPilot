"""Bind generated documents to one posting and detect stale or changed uploads."""

import hashlib
import json
import re
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


def write_manifest(job: dict, text_path: Path, *, kind: str, approved: bool) -> Path:
    """Write only after text/PDF generation. Missing PDFs cannot be uploaded."""
    text_path = Path(text_path)
    pdf_path = text_path.with_suffix(".pdf")
    manifest = {
        "version": 1, "job_url": job["url"], "title": job.get("title"),
        "company": job.get("company") or job.get("site"), "kind": kind,
        "approved": approved, "text_sha256": _digest(text_path),
        "pdf_sha256": _digest(pdf_path) if pdf_path.is_file() else None,
    }
    path = text_path.with_suffix(".manifest.json")
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def verify_artifact(job: dict, text_path: str | Path, *, kind: str) -> Path:
    """Return verified PDF path or raise ValueError before an upload occurs."""
    text_path = Path(text_path)
    pdf_path = text_path.with_suffix(".pdf")
    try:
        manifest = json.loads(text_path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
        if manifest.get("job_url") != job.get("url") or manifest.get("kind") != kind:
            raise ValueError("Document belongs to another job or document type")
        if manifest.get("approved") is not True:
            raise ValueError("Document has not passed validation")
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
    if not manifest.get("approved") or manifest.get("text_sha256") != _digest(text_path):
        raise ValueError("Cannot certify a PDF generated from unapproved or changed text")
    manifest["pdf_sha256"] = _digest(text_path.with_suffix(".pdf"))
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
