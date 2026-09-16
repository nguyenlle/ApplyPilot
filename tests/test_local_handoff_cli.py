"""Local preparation rejects invalid inputs without provider bootstrap or apply."""

import pytest
from typer.testing import CliRunner

from applypilot import cli, local_handoff


@pytest.mark.parametrize("command,function,args", [
    ("local-export", "export_job", ["--url", "https://example.org/jobs/one"]),
    ("local-render", "render_result", ["handoff.json", "result.json"]),
    ("local-import", "import_result", ["handoff.json", "result.json", "--review", "review.json"]),
])
def test_rejected_local_command_is_nonzero_without_provider_bootstrap(monkeypatch, command, function, args):
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "LLM_URL"):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Local preparation must not bootstrap provider execution")

    def reject(*args):
        raise ValueError("Stale handoff [untrusted]")

    monkeypatch.setattr(cli, "_bootstrap", forbidden)
    monkeypatch.setattr(local_handoff, function, reject)
    result = CliRunner().invoke(cli.app, [command, *args])
    assert result.exit_code == 1
    assert "Local preparation rejected: Stale handoff [untrusted]" in result.output


def test_local_import_requires_explicit_review_argument():
    result = CliRunner().invoke(cli.app, ["local-import", "handoff.json", "result.json"])
    assert result.exit_code != 0
