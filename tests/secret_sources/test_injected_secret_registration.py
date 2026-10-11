"""The external-secret-source apply path must register what it injects for redaction.

A source (vault CLI, Bitwarden, …) writes real credential values into a profile's environment. Nothing
watches that injection the way the browser vault fill path watches a typed value, so the value-level
redaction registry stayed empty for it and any tool output whose *command shape* the classifier cannot
recognize (a copy of an env dump read with ``sed``, a remote file printed over ``ssh``) put the value
into model context and the persisted transcript. These tests drive the real apply path with a fake
source and assert the injected value is scrubbed afterwards.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.redact import clear_vault_redaction_values, redact_terminal_output
from agent.secret_sources import registry as reg
from agent.secret_sources.base import SECRET_SOURCE_API_VERSION, FetchResult, SecretSource
from hermes_cli import env_loader

INJECTED_NAME = "FAKE_SERVICE_TOKEN"
INJECTED_VALUE = "fak_" + "a1b2c3d4" * 5  # 40 chars, no vendor prefix → only value registration catches it
SED_DUMP_COMMAND = "sed -n '60,185p' /tmp/shell-env.txt"


def _make_source(name="dummy", secrets=None):
    class _Src(SecretSource):
        def fetch(self, cfg, home_path):
            res = FetchResult()
            res.secrets = dict(secrets or {})
            return res

        def override_existing(self, cfg):
            return True

        def protected_env_vars(self, cfg):
            return frozenset()

    _Src.name = name
    _Src.label = name.title()
    _Src.shape = "mapped"
    _Src.scheme = None
    _Src.api_version = SECRET_SOURCE_API_VERSION
    return _Src()


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Empty registry, no builtin auto-load, and no once-per-home latch from another test."""
    reg._reset_registry_for_tests()
    monkeypatch.setattr(reg, "_ensure_builtin_sources", lambda: None)
    # Only the latches this base actually has: the snapshot names differ between bases.
    for attr in ("_APPLIED_HOMES", "_SECRET_SOURCES", "_SOURCE_SUPPLIED_NAMES",
                 "_SECRET_SOURCE_VALUES_BY_HOME", "_SECRET_SOURCE_RESTORE_BY_HOME",
                 "_SECRET_SOURCE_WRITES_BY_HOME"):
        if hasattr(env_loader, attr):
            monkeypatch.setattr(env_loader, attr, type(getattr(env_loader, attr))())
    monkeypatch.delenv(INJECTED_NAME, raising=False)
    clear_vault_redaction_values()
    yield
    clear_vault_redaction_values()
    reg._reset_registry_for_tests()


def _home_with_source(tmp_path) -> Path:
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        f"secrets:\n  dummy:\n    enabled: true\n", encoding="utf-8")
    return home


def test_applied_source_value_is_scrubbed_from_unclassifiable_output(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = _home_with_source(tmp_path).resolve()
    assert reg.register_source(_make_source(secrets={INJECTED_NAME: INJECTED_VALUE}))

    token = set_hermes_home_override(str(home))
    try:
        env_loader._apply_external_secret_sources(home)

        assert env_loader.get_secret_source(INJECTED_NAME) == "dummy"

        out = redact_terminal_output(
            f"{INJECTED_NAME}={INJECTED_VALUE}\nUSER=hermes\n", SED_DUMP_COMMAND)

        assert INJECTED_VALUE not in out
        assert "redacted" in out
    finally:
        reset_hermes_home_override(token)


def test_registration_does_not_mask_unrelated_prose(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = _home_with_source(tmp_path).resolve()
    assert reg.register_source(_make_source(secrets={"NC_ADMIN_USER": "hermes"}))

    token = set_hermes_home_override(str(home))
    try:
        env_loader._apply_external_secret_sources(home)

        out = redact_terminal_output("cd /home/hermes/.hermes && ls", SED_DUMP_COMMAND)

        assert "hermes" in out
    finally:
        reset_hermes_home_override(token)
