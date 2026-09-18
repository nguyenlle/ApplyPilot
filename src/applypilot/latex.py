"""Hash-bound external LaTeX builds. This module never executes TeX or a shell.

Build metadata records an author's claim, not proof that a compiler executed.
Independent source, log, extracted-text and all-page visual review is required.
"""
import stat
from pathlib import Path

from applypilot import config

TEMPLATE_NAME = "resume-template.tex"
BUILD_NAMES = (TEMPLATE_NAME, "resume-tailored.tex", "latex-build.json", "latex-build.log")
REVIEW_KEYS = ("template_layout_checked", "source_matches_pdf_checked", "build_log_checked", "ats_text_checked")
BUILD_KEYS = ("version", "renderer", "handoff_sha256", "result_sha256", "template_sha256",
              "tex_sha256", "pdf_sha256", "log_sha256", "compiler", "compiler_version",
              "argv", "shell_escape", "exit_code")


def template_bytes(path: Path) -> bytes:
    """Read the configured original outside private state without following links."""
    if ".." in path.parts:
        raise ValueError("Template path traversal is forbidden")
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("Template paths must not contain links or junctions")
    if path.stat().st_nlink > 1:
        raise ValueError("Template must not be a hard link")
    content = path.read_bytes()
    if not content.strip():
        raise ValueError("Required LaTeX template is empty")
    return content


def reject_generic_resume() -> None:
    if config.required_resume_template() is not None:
        raise ValueError("Required LaTeX resume template: export a fresh local handoff, compile the template, "
                         "record_latex_build, render the cover letter, and obtain independent review; "
                         "generic resume rendering is forbidden")


def validate_build(folder: Path, handoff_hash: str, result_hash: str, template_hash: str) -> dict:
    from applypilot import local_handoff as h

    build = h.read(folder / "latex-build.json")
    h._keys(build, BUILD_KEYS, "LaTeX build")
    if (type(build["version"]) is not int or build["version"] != 1 or build["renderer"] != "latex"
            or build["shell_escape"] is not False or type(build["exit_code"]) is not int or build["exit_code"] != 0):
        raise h.HandoffValidationError("Successful LaTeX build without shell escape required")
    for key, expected in (("handoff_sha256", handoff_hash), ("result_sha256", result_hash),
                          ("template_sha256", template_hash)):
        if build[key] != expected:
            raise h.HandoffValidationError("LaTeX build belongs to different or stale inputs")
    for key, filename in (("template_sha256", TEMPLATE_NAME), ("tex_sha256", "resume-tailored.tex"),
                          ("pdf_sha256", "resume-tailored.pdf"), ("log_sha256", "latex-build.log")):
        if build[key] != h.digest(folder / filename):
            raise h.HandoffValidationError("LaTeX source, template, log or PDF changed since build")
    for key in ("compiler", "compiler_version"):
        h._text(build[key], key)
    argv = h._strings(build["argv"], "LaTeX build argv")
    allowed_flags = {"-no-shell-escape", "--no-shell-escape", "-halt-on-error", "--halt-on-error",
                     "-file-line-error", "--file-line-error", "-interaction=nonstopmode",
                     "--interaction=nonstopmode", "-interaction=batchmode", "--interaction=batchmode"}
    if (len(argv) < 3 or argv[0] != build["compiler"] or argv[-1] != "resume-tailored.tex"
            or not {"-no-shell-escape", "--no-shell-escape"}.intersection(argv)
            or set(argv[1:-1]) - allowed_flags
            or Path(build["compiler"]).stem.lower() not in {"pdflatex", "xelatex", "lualatex"}):
        raise h.HandoffValidationError("LaTeX argv must identify compiler, no-shell-escape and pinned source")
    for filename in ("resume-tailored.tex", "latex-build.log"):
        if not h.private_path(folder / filename).read_bytes().strip():
            raise h.HandoffValidationError("LaTeX source and build log must be nonempty")
    if not h.private_path(folder / "resume-tailored.pdf").read_bytes().startswith(b"%PDF-"):
        raise h.HandoffValidationError("LaTeX output must be a PDF")
    return build


def record_latex_build(handoff_path, result_path, *, compiler: str, compiler_version: str, argv: list[str]) -> Path:
    """Record hashes after an external no-shell-escape compile; never approve it."""
    from applypilot import local_handoff as h

    folder, handoff, _, _, _ = h.validate_result(handoff_path, result_path)
    if TEMPLATE_NAME not in handoff["inputs"]:
        raise h.HandoffValidationError("LaTeX build requires a fresh template-bound handoff")
    for name in ("review.json", "latex-build.json", "resume-tailored.manifest.json", "cover-letter.manifest.json"):
        if h.private_path(folder / name).exists():
            raise h.HandoffValidationError("Build/review already recorded; export a fresh handoff")
    build = {"version": 1, "renderer": "latex", "handoff_sha256": h.digest(handoff_path),
             "result_sha256": h.digest(result_path), "template_sha256": h.digest(folder / TEMPLATE_NAME),
             "tex_sha256": h.digest(folder / "resume-tailored.tex"), "pdf_sha256": h.digest(folder / "resume-tailored.pdf"),
             "log_sha256": h.digest(folder / "latex-build.log"), "compiler": compiler,
             "compiler_version": compiler_version, "argv": argv, "shell_escape": False, "exit_code": 0}
    path = h.private_path(folder / "latex-build.json")
    h.write(path, build)
    try:
        validate_build(folder, h.digest(handoff_path), h.digest(result_path), handoff["inputs"][TEMPLATE_NAME]["sha256"])
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path
