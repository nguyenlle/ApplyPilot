import copy
import json
from pathlib import Path

import pytest

from applypilot import config, diagnostics


def candidate():
    return {
        "personal": {"full_name": "Test Candidate", "email": "candidate@domain.test"},
        "work_authorization": {"legally_authorized_to_work": None, "require_sponsorship": None},
        "eeo_voluntary": {"gender": None, "veteran_status": None},
    }


def test_unknown_personal_answers_are_not_guessed_or_mutated():
    profile = candidate()
    before = copy.deepcopy(profile)
    assert config.validate_profile_for_application(profile) is profile
    assert profile == before
    assert profile["work_authorization"]["require_sponsorship"] is None


@pytest.mark.parametrize("profile", [
    {"name": "Firstname Lastname", "email": "you@example.com"},
    {"personal": {"full_name": "YOUR_LEGAL_NAME", "email": "candidate@domain.test"}},
    {"personal": {"full_name": "Person", "email": "your.email@example.com"}},
    {"personal": {"full_name": "Person", "email": "invalid"}},
    {"personal": {"full_name": "Person", "email": "candidate@domain.test"}, "screening": []},
])
def test_placeholder_or_malformed_profile_is_rejected(profile):
    with pytest.raises(ValueError, match="Invalid application profile"):
        config.validate_profile_for_application(profile)


def test_sample_profile_has_no_supplied_legal_or_eeo_answers():
    path = Path(__file__).parents[1] / "profile.example.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    for section in ("work_authorization", "eeo_voluntary", "screening", "compensation"):
        assert all(value is None for value in profile[section].values())
    assert config.profile_safety_reasons(profile)


def test_claude_resolver_finds_native_install(tmp_path, monkeypatch):
    native = tmp_path / ".local" / "bin" / "claude.exe"
    native.parent.mkdir(parents=True)
    native.touch()
    monkeypatch.delenv("CLAUDE_PATH", raising=False)
    monkeypatch.setattr(config.shutil, "which", lambda _: None)
    monkeypatch.setattr(config.Path, "home", lambda: tmp_path)
    assert config.resolve_claude() == str(native)


def test_invalid_claude_override_does_not_fall_back(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PATH", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(config.shutil, "which", lambda _: "other-claude")
    assert config.resolve_claude() is None


def test_claude_override_wins(monkeypatch, tmp_path):
    selected = tmp_path / "selected.exe"
    selected.touch()
    monkeypatch.setenv("CLAUDE_PATH", str(selected))
    assert config.resolve_claude() == str(selected.resolve())


def test_tier_three_requires_node_and_npx(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(config, "configured_llm_provider", lambda: "openai")
    monkeypatch.setattr(config, "get_chrome_path", lambda: "chrome.exe")
    monkeypatch.setattr(config.shutil, "which", lambda name: "node.exe" if name == "node" else None)
    assert config.get_tier() == 2
    monkeypatch.setattr(config.shutil, "which", lambda name: name)
    assert config.get_tier() == 3


@pytest.fixture
def ready_local_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    (tmp_path / "application_adapters.json").write_text(
        json.dumps({"adapters": [{"kind": "native_html_v1", "application_url": "https://jobs.example.test/apply"}]}),
        encoding="utf-8",
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(candidate()), encoding="utf-8")
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Verified candidate experience and education. " * 10, encoding="utf-8")
    search_path = tmp_path / "searches.yaml"
    search_path.write_text("queries:\n  - query: engineer\nlocations:\n  - location: Remote\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "RESUME_PATH", resume_path)
    monkeypatch.setattr(config, "SEARCH_CONFIG_PATH", search_path)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(config, "get_chrome_path", lambda: "chrome.exe")
    monkeypatch.setattr(diagnostics, "_module_available", lambda _: True)
    monkeypatch.setattr(diagnostics, "_chromium_available", lambda: True)
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: name)
    for name in ("LLM_URL", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-private-never-display")


def test_doctor_missing_credentials_fails(ready_local_setup, monkeypatch):
    assert diagnostics.run_diagnostics()["ok"]
    monkeypatch.delenv("OPENAI_API_KEY")
    assert not diagnostics.run_diagnostics()["ok"]
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-gemini-key")
    assert diagnostics.run_diagnostics(required_tier=2)["ok"]
    assert not diagnostics.run_diagnostics(required_tier=3)["ok"]


def test_doctor_and_tier_do_not_require_claude(ready_local_setup, monkeypatch):
    monkeypatch.setattr(config, "resolve_claude", lambda: pytest.fail("Claude must not be queried"))
    assert diagnostics.run_diagnostics()["ok"]
    assert config.get_tier() == 3


def test_apply_model_uses_separate_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_URL", raising=False)
    monkeypatch.delenv("APPLY_MODEL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "chosen-model")
    assert config.apply_model() == "chosen-model"
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-key")
    assert config.apply_model() == "gpt-4o-mini"
    monkeypatch.setenv("APPLY_MODEL", "browser-model")
    assert config.apply_model() == "browser-model"


def test_doctor_requires_adapter_only_for_live_tier(ready_local_setup):
    (config.APP_DIR / "application_adapters.json").unlink()
    assert not diagnostics.run_diagnostics()["ok"]
    assert diagnostics.run_diagnostics(required_tier=2)["ok"]


def test_preview_adapter_does_not_unlock_live_submission(ready_local_setup):
    (config.APP_DIR / "application_adapters.json").write_text(
        json.dumps({"adapters": [{"kind": "archer_greenhouse_preview_v1"}]}), encoding="utf-8")
    assert not diagnostics.run_diagnostics()["ok"]


def test_doctor_does_not_reveal_keys_or_private_endpoint(ready_local_setup, monkeypatch):
    result = diagnostics.run_diagnostics()
    assert "sk-private-never-display" not in json.dumps(result)
    monkeypatch.setenv("LLM_URL", "https://secret-user:secret-password@private.example/v1?token=secret-token")
    result = diagnostics.run_diagnostics()
    serialized = json.dumps(result)
    for secret in ("secret-user", "secret-password", "secret-token", "private.example"):
        assert secret not in serialized


def test_doctor_rejects_empty_resume_and_example_fallback(ready_local_setup):
    config.RESUME_PATH.write_text("", encoding="utf-8")
    config.SEARCH_CONFIG_PATH.unlink()
    result = diagnostics.run_diagnostics()
    assert not result["ok"]
    statuses = {item["name"]: item["ok"] for item in result["checks"]}
    assert not statuses["resume.txt"]
    assert not statuses["searches.yaml"]
