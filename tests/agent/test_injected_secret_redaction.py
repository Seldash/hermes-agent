"""Injected-secret redaction: values a profile puts in its own env must be masked in tool output.

The vault-fill path registers the values it injects (``register_vault_redaction_value``), but a value
that arrives through the profile environment (``.env``, an external secret source) never transits a
tool that watches it. Command-shape classification cannot close that gap: it keys on a read of
``.env`` / shell rc / ``config.yaml``, while the leaks on record came from shapes it cannot classify
(a copy of an env dump read with ``sed``, a remote config file printed over ``ssh``, a python
heredoc). These tests pin the value-level boundary for that class, including the floor that keeps a
short identifier from being scrubbed out of ordinary prose.
"""

from __future__ import annotations

import pytest

import agent.redact as redact_mod
from agent.redact import (
    clear_vault_redaction_values,
    redact_terminal_output,
    register_injected_secret_values,
)

# A Tushare-shaped token (56 hex) and an Aliyun-shaped secret (30 base64): neither carries a vendor
# prefix, so no built-in pattern matches them -- only value registration can.
HEX_TOKEN = "0123456789abcdef" * 3 + "0123456789ab"
B64_SECRET = "aB3dEfGh1JkLmNoPqRsTuVwXyZ1234"
# A leak shape from the incident record: a copy of an env dump read back with sed. Not an env-dump
# command and not a secret-file read, so the assignment pass is skipped by design (code_file=True).
SED_DUMP_COMMAND = "sed -n '60,185p' /tmp/shell-env.txt"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_vault_redaction_values()
    yield
    clear_vault_redaction_values()


def test_credential_named_value_is_scrubbed_from_unclassifiable_output():
    """A credential-named env value must not survive a command the classifier cannot type."""
    registered = register_injected_secret_values({"TUSHARE_TOKEN": HEX_TOKEN})
    assert registered == 1

    out = redact_terminal_output(f"TUSHARE_TOKEN={HEX_TOKEN}\nUSER=hermes\n", SED_DUMP_COMMAND)

    assert HEX_TOKEN not in out
    assert "redacted" in out


def test_opaque_value_is_registered_even_without_credential_name():
    """Shape is enough: an opaque 30-char secret under an unrelated name still leaves model context."""
    assert register_injected_secret_values({"SOME_SERVICE": B64_SECRET}) == 1

    out = redact_terminal_output(f"config: {B64_SECRET}", SED_DUMP_COMMAND)

    assert B64_SECRET not in out


def test_short_identifier_is_never_registered():
    """The scrub is an exact-substring replace: a short identifier would rewrite ordinary prose."""
    assert register_injected_secret_values({"NC_ADMIN_USER": "hermes"}) == 0

    out = redact_terminal_output("cd /home/hermes/.hermes && python3 vault-env.py", SED_DUMP_COMMAND)

    assert "hermes" in out


def test_value_below_floor_is_skipped_even_with_credential_name():
    """Floor applies to credential-named values too (7 chars here)."""
    assert register_injected_secret_values({"DB_KEY": "abc1234"}) == 0

    assert "abc1234" in redact_terminal_output("DB_KEY=abc1234", SED_DUMP_COMMAND)


def test_plain_identifier_valued_credential_name_is_skipped():
    """A credential-shaped *name* with a plain value (no shape, below floor) is not registered."""
    assert register_injected_secret_values({"API_KEY": "local"}) == 0


def test_registration_counts_only_what_it_registers():
    assert register_injected_secret_values(
        {"A_TOKEN": HEX_TOKEN, "NC_ADMIN_USER": "hermes", "B_KEY": ""}
    ) == 1


def test_empty_and_non_string_inputs_are_ignored():
    assert register_injected_secret_values({}) == 0
    assert register_injected_secret_values(None) == 0
    assert register_injected_secret_values({"C_TOKEN": None, "D_TOKEN": 123456789}) == 0


def test_registered_value_survives_a_forced_redaction_pass():
    """The value registry is not gated by ``security.redact_secrets``: force= either way."""
    register_injected_secret_values({"E_TOKEN": HEX_TOKEN})

    assert HEX_TOKEN not in redact_terminal_output(f"E_TOKEN={HEX_TOKEN}", SED_DUMP_COMMAND, force=True)


def test_clear_drops_registered_values():
    register_injected_secret_values({"F_TOKEN": HEX_TOKEN})
    assert HEX_TOKEN not in redact_terminal_output(f"F_TOKEN={HEX_TOKEN}", SED_DUMP_COMMAND)

    clear_vault_redaction_values()

    assert HEX_TOKEN in redact_terminal_output(f"F_TOKEN={HEX_TOKEN}", SED_DUMP_COMMAND)


def test_registration_failure_never_propagates(monkeypatch):
    """A redaction-registry problem must not break the caller (startup path)."""

    def _boom(_value):
        raise RuntimeError("registry down")

    monkeypatch.setattr(redact_mod, "register_vault_redaction_value", _boom)

    assert register_injected_secret_values({"G_TOKEN": HEX_TOKEN}) == 0
