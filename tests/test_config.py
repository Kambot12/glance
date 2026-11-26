"""Config validation, including the no-secrets-in-config rule."""
import pytest
import yaml

from glance.config import AppConfig, ConfigError, ProviderConfig, load_config

MINIMAL = {
    "modes": [
        {"name": "summarize", "hotkey": "<cmd>+<shift>+1", "system_prompt": "do it"}
    ]
}


def test_minimal_config_gets_sane_defaults():
    cfg = AppConfig.model_validate(MINIMAL)
    assert cfg.provider.base_url.startswith("http://127.0.0.1:11434")
    assert cfg.server.lan is False
    assert cfg.capture.jpeg_quality == 70
    assert cfg.privacy.fail_closed_on_unknown_title is True


def test_shipped_example_config_is_valid():
    cfg = load_config("config.example.yaml")
    assert [m.name for m in cfg.modes] == ["summarize", "explain_code", "analyze_dashboard"]
    assert cfg.provider.api_key_env is None  # no secret, not even a name, by default


def test_at_least_one_mode_required():
    with pytest.raises(Exception):
        AppConfig.model_validate({"modes": []})


def test_duplicate_mode_names_rejected():
    raw = {"modes": [dict(MINIMAL["modes"][0]), dict(MINIMAL["modes"][0])]}
    raw["modes"][1]["hotkey"] = "<cmd>+<shift>+2"
    with pytest.raises(Exception, match="duplicate mode names"):
        AppConfig.model_validate(raw)


def test_hotkey_bound_twice_rejected():
    raw = {
        "modes": [
            {"name": "a", "hotkey": "<cmd>+<shift>+1", "system_prompt": "x"},
            {"name": "b", "hotkey": "<cmd>+<shift>+1", "system_prompt": "y"},
        ]
    }
    with pytest.raises(Exception, match="bound more than once"):
        AppConfig.model_validate(raw)


def test_pause_hotkey_cannot_collide_with_a_mode():
    raw = {"modes": [dict(MINIMAL["modes"][0])], "pause_hotkey": "<cmd>+<shift>+1"}
    with pytest.raises(Exception, match="bound more than once"):
        AppConfig.model_validate(raw)


@pytest.mark.parametrize("bad", ["cmd+1", "<cmd>+", "", "<cmd> + 1"])
def test_malformed_hotkey_rejected(bad):
    raw = {"modes": [{"name": "a", "hotkey": bad, "system_prompt": "x"}]}
    with pytest.raises(Exception):
        AppConfig.model_validate(raw)


def test_unknown_key_rejected_rather_than_ignored():
    raw = dict(MINIMAL, capture={"jpeg_qualty": 70})  # typo
    with pytest.raises(Exception):
        AppConfig.model_validate(raw)


# -- the secrets rule --------------------------------------------------------

def test_api_key_env_accepts_an_env_var_name():
    assert ProviderConfig(api_key_env="OPENAI_API_KEY").api_key_env == "OPENAI_API_KEY"


@pytest.mark.parametrize(
    "literal",
    ["sk-abc123def456", "AIzaSyFAKEfakefake", "my-secret-key", "openai_api_key"],
)
def test_a_pasted_secret_is_rejected(literal):
    """config.yaml must hold env var NAMES, never values."""
    with pytest.raises(Exception, match="environment variable NAME"):
        ProviderConfig(api_key_env=literal)


def test_resolve_key_reads_the_environment(monkeypatch):
    monkeypatch.setenv("GLANCE_TEST_KEY", "s3cret")
    assert ProviderConfig(api_key_env="GLANCE_TEST_KEY").resolve_key() == "s3cret"


def test_resolve_key_explains_a_missing_variable(monkeypatch):
    monkeypatch.delenv("GLANCE_ABSENT_KEY", raising=False)
    with pytest.raises(RuntimeError, match="not set"):
        ProviderConfig(api_key_env="GLANCE_ABSENT_KEY").resolve_key()


def test_no_key_needed_for_local_ollama():
    assert ProviderConfig().resolve_key() == "not-needed"


# -- loading -----------------------------------------------------------------

def test_missing_file_gives_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="no config at"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml_gives_a_clear_error(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("modes: [: :\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(p)


def test_validation_error_names_the_file(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"modes": []}))
    with pytest.raises(ConfigError, match="failed validation"):
        load_config(p)
