"""Synthetic external-build provenance; no compiler, model or employer calls."""
from unittest.mock import Mock

import pytest
from test_local_handoff import bundle, import_bundle, job, write_json
from test_local_handoff import local as local_fixture

from applypilot import artifacts, config, latex
from applypilot import local_handoff as h
from applypilot.scoring import pdf, tailor

local = local_fixture


def configure(local):
    template = local.private.parent / "original-template.tex"
    template.write_text("% Original synthetic template attribution\n\\documentclass[11pt,letterpaper]{article}", encoding="utf-8")
    write_json(local.private / "resume-rendering.json", {"renderer": "latex", "template_path": str(template)})
    return template


def latex_bundle(local):
    template = configure(local)
    b = bundle(local)
    (b.folder / "resume-tailored.tex").write_bytes(template.read_bytes() + b"\n% synthetic build fixture")
    (b.folder / "latex-build.log").write_text("Synthetic successful compiler log; unit test did not compile TeX.")
    # record_latex_build deliberately refuses to change already reviewed bytes.
    b.review_path.unlink()
    latex.record_latex_build(b.path, b.result_path, compiler="pdflatex", compiler_version="synthetic test",
                            argv=["pdflatex", "-no-shell-escape", "-halt-on-error", "resume-tailored.tex"])
    b.review["files"] = {name: h.digest(b.folder / name) for name in h.artifact_names(h.read(b.path))}
    b.review["latex"] = {key: True for key in latex.REVIEW_KEYS}
    write_json(b.review_path, b.review)
    return b, template


def reject_clean(local, b):
    before = list(local.conn.iterdump())
    with pytest.raises((ValueError, OSError)):
        import_bundle(b)
    assert list(local.conn.iterdump()) == before
    assert not list(b.folder.glob("*.manifest.json"))


def test_external_build_import_binds_all_files_and_preserves_application_state(local):
    b, _ = latex_bundle(local)
    before = job(local)
    receipt = import_bundle(b)
    assert set(receipt["files"]) == set(h.ARTIFACT_NAMES + latex.BUILD_NAMES)
    after = job(local)
    for key in ("apply_status", "applied_at", "apply_attempts", "claim_token", "verification_state"):
        assert after[key] == before[key]
    assert artifacts.verify_artifact(after, after["tailored_resume_path"], kind="resume") == b.folder / "resume-tailored.pdf"
    with pytest.raises(ValueError):
        import_bundle(b)


@pytest.mark.parametrize("name", latex.BUILD_NAMES + ("resume-tailored.pdf",))
def test_changed_build_artifact_fails_even_after_review_file_rehash(local, name):
    b, _ = latex_bundle(local)
    path = b.folder / name
    path.write_bytes(path.read_bytes() + b"\nmodified")
    b.review["files"][name] = h.digest(path)
    write_json(b.review_path, b.review)
    reject_clean(local, b)


@pytest.mark.parametrize("change", ["handoff", "result", "shell", "exit_bool", "exit_failure", "extra",
                                   "argv_enable", "argv_no_disable", "wrong_source", "compiler", "version_bool"])
def test_build_metadata_rejects_changed_input_or_unsafe_shape(local, change):
    b, _ = latex_bundle(local)
    build = h.read(b.folder / "latex-build.json")
    if change in {"handoff", "result"}: build[change + "_sha256"] = "0" * 64
    elif change == "shell": build["shell_escape"] = True
    elif change == "exit_bool": build["exit_code"] = False
    elif change == "exit_failure": build["exit_code"] = 1
    elif change == "extra": build["unreviewed"] = "field"
    elif change == "argv_enable": build["argv"].insert(1, "-shell-escape")
    elif change == "argv_no_disable": build["argv"].remove("-no-shell-escape")
    elif change == "wrong_source": build["argv"][-1] = "other.tex"
    elif change == "compiler": build["compiler"] = "other"
    else: build["version"] = True
    write_json(b.folder / "latex-build.json", build)
    b.review["files"]["latex-build.json"] = h.digest(b.folder / "latex-build.json")
    write_json(b.review_path, b.review)
    reject_clean(local, b)


@pytest.mark.parametrize("change", ["missing", "modified", "config_removed", "different_template"])
def test_current_required_template_drift_blocks_import(local, change):
    b, template = latex_bundle(local)
    if change == "missing": template.unlink()
    elif change == "modified": template.write_text("Changed template")
    elif change == "config_removed": (local.private / "resume-rendering.json").unlink()
    else:
        other = local.private.parent / "other-template.tex"
        other.write_text("Different template")
        write_json(local.private / "resume-rendering.json", {"renderer": "latex", "template_path": str(other)})
    reject_clean(local, b)


@pytest.mark.parametrize("key", latex.REVIEW_KEYS)
def test_each_independent_latex_review_attestation_required(local, key):
    b, _ = latex_bundle(local)
    b.review["latex"][key] = 1
    write_json(b.review_path, b.review)
    reject_clean(local, b)


def test_review_cannot_omit_latex_provenance(local):
    b, _ = latex_bundle(local)
    del b.review["latex"]
    b.review["files"] = {k: v for k, v in b.review["files"].items() if k in h.ARTIFACT_NAMES}
    write_json(b.review_path, b.review)
    reject_clean(local, b)


@pytest.mark.parametrize("name", latex.BUILD_NAMES)
def test_committed_provenance_changes_invalidate_resume_and_cover(local, name):
    b, _ = latex_bundle(local)
    import_bundle(b)
    (b.folder / name).write_bytes(b"changed after import")
    for kind, field in (("resume", "tailored_resume_path"), ("cover_letter", "cover_letter_path")):
        with pytest.raises(ValueError):
            artifacts.verify_artifact(job(local), job(local)[field], kind=kind)


def test_committed_receipt_does_not_require_original_template_forever(local):
    b, template = latex_bundle(local)
    import_bundle(b)
    template.unlink()
    assert artifacts.verify_job_artifacts(job(local))["resume"] == b.folder / "resume-tailored.pdf"


def test_legacy_receipt_remains_verifiable_after_requiring_template(local):
    b = bundle(local)
    import_bundle(b)
    configure(local).unlink()
    assert artifacts.verify_job_artifacts(job(local))["resume"] == b.folder / "resume-tailored.pdf"


def test_latex_render_preserves_external_pdf_and_only_renders_cover(local, monkeypatch):
    b, _ = latex_bundle(local)
    b.review_path.unlink()
    before = (b.folder / "resume-tailored.pdf").read_bytes()
    generic, letter = Mock(side_effect=AssertionError("generic resume called")), Mock()
    monkeypatch.setattr(pdf, "convert_to_pdf", generic)
    monkeypatch.setattr(pdf, "convert_letter_to_pdf", letter)
    h.render_result(b.path, b.result_path)
    generic.assert_not_called()
    letter.assert_called_once_with(b.folder / "cover-letter.txt")
    assert (b.folder / "resume-tailored.pdf").read_bytes() == before


def test_missing_build_prevents_text_or_cover_writes(local):
    configure(local)
    b = bundle(local)
    before = {p.name: p.read_bytes() for p in b.folder.iterdir() if p.is_file()}
    with pytest.raises(ValueError): h.render_result(b.path, b.result_path)
    assert {p.name: p.read_bytes() for p in b.folder.iterdir() if p.is_file()} == before


def test_generic_resume_rejects_before_overwriting_pdf(local, monkeypatch):
    b = bundle(local)
    configure(local)
    previous = (b.folder / "resume-tailored.pdf").read_bytes()
    monkeypatch.setattr(pdf, "render_pdf", Mock(side_effect=AssertionError("must not render")))
    with pytest.raises(ValueError, match="LaTeX"):
        pdf.convert_to_pdf(b.folder / "resume-tailored.txt")
    assert (b.folder / "resume-tailored.pdf").read_bytes() == previous


@pytest.mark.parametrize("kind", ["resume", "cover"])
def test_imported_pdf_cannot_be_overwritten_by_generic_renderer(local, monkeypatch, kind):
    b = bundle(local)
    import_bundle(b)
    filename = "resume-tailored" if kind == "resume" else "cover-letter"
    previous = (b.folder / (filename + ".pdf")).read_bytes()
    monkeypatch.setattr(pdf, "render_pdf", Mock(side_effect=AssertionError("must not render")))
    with pytest.raises(ValueError, match="fresh handoff"):
        (pdf.convert_to_pdf if kind == "resume" else pdf.convert_letter_to_pdf)(b.folder / (filename + ".txt"))
    assert (b.folder / (filename + ".pdf")).read_bytes() == previous


@pytest.mark.parametrize("renderer,html", [("resume", False), ("cover", False), ("resume", True)])
def test_explicit_output_cannot_overwrite_another_imported_document(local, monkeypatch, renderer, html):
    b = bundle(local)
    import_bundle(b)
    fresh = local.private / "unrelated.txt"
    fresh.write_text("Dear Team,\nSynthetic cover." if renderer == "cover" else "Synthetic\nEngineer\nSUMMARY\nCAD")
    target = b.folder / "resume-tailored.pdf"
    previous = target.read_bytes()
    monkeypatch.setattr(pdf, "render_pdf", Mock(side_effect=AssertionError("must not render")))
    with pytest.raises(ValueError, match="fresh handoff"):
        if renderer == "cover": pdf.convert_letter_to_pdf(fresh, output_path=target)
        else: pdf.convert_to_pdf(fresh, output_path=target, html_only=html)
    assert target.read_bytes() == previous


def test_automated_tailoring_stops_before_model_or_artifact_write(local, monkeypatch):
    configure(local)
    monkeypatch.setattr(tailor, "RESUME_PATH", config.RESUME_PATH)
    monkeypatch.setattr(tailor, "get_jobs_by_stage", lambda **_: [job(local)])
    model = Mock(side_effect=AssertionError("model must not run"))
    monkeypatch.setattr(tailor, "tailor_resume", model)
    with pytest.raises(ValueError, match="LaTeX"): tailor.run_tailoring()
    model.assert_not_called()


@pytest.mark.parametrize("raw", ['{}', '[]', '{"renderer":"latex","renderer":"html","template_path":"x"}',
                                 '{"renderer":"latex","template_path":"relative.tex"}', 'null'])
def test_invalid_required_template_config_never_falls_back(local, raw):
    (local.private / "resume-rendering.json").write_text(raw)
    with pytest.raises(ValueError): config.required_resume_template()


def test_template_hardlink_and_external_source_symlink_rejected(local):
    template = configure(local)
    linked = local.private.parent / "linked-template.tex"
    linked.hardlink_to(template)
    with pytest.raises(ValueError, match="hard link"): h.export_job(local.url)


def test_build_cannot_overwrite_reviewed_or_existing_metadata(local):
    b, _ = latex_bundle(local)
    previous = (b.folder / "latex-build.json").read_bytes()
    with pytest.raises(ValueError):
        latex.record_latex_build(b.path, b.result_path, compiler="pdflatex", compiler_version="test",
                                argv=["pdflatex", "-no-shell-escape", "resume-tailored.tex"])
    assert (b.folder / "latex-build.json").read_bytes() == previous


def test_low_level_renderer_refuses_imported_output_before_browser_launch(local):
    b = bundle(local)
    import_bundle(b)
    target = b.folder / "resume-tailored.pdf"
    before = target.read_bytes()
    with pytest.raises(ValueError, match="fresh handoff"):
        pdf.render_pdf("<p>different</p>", str(target))
    assert target.read_bytes() == before


def test_output_hardlink_cannot_overwrite_imported_pdf(local):
    b = bundle(local)
    import_bundle(b)
    target = b.folder / "resume-tailored.pdf"
    alias = local.private / "alias.pdf"
    alias.hardlink_to(target)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="hard-linked"):
        artifacts.assert_can_render(alias)
    assert target.read_bytes() == before


def test_explicit_output_cannot_overwrite_committed_build_log(local):
    b, _ = latex_bundle(local)
    import_bundle(b)
    log = b.folder / "latex-build.log"
    previous = log.read_bytes()
    with pytest.raises(ValueError, match="fresh handoff"):
        artifacts.assert_can_render(log)
    assert log.read_bytes() == previous
