"""Credentials must never persist in the shared bash session snapshot.

The snapshot (``HERMES_HOME/cache/scratch/hermes-snap-*.sh``) is a file every later command
``source``s. It used to carry whatever ``export -p`` showed the login shell, so any provider
or tool key the harness had in its process env — ``BOCHA_API_KEY``, ``TUSHARE_TOKEN``,
``ALIYUN_ACCESS_KEY_SECRET`` … — was written to disk on every command and re-exported into
every later session's shell (and, under a multiplexed gateway, into another profile's shell).

``_SNAPSHOT_SECRET_NAME_RE`` drops credential-shaped names at both dump sites. Functionality
is preserved because those values are re-injected onto every command's process env by the
spawn layer — the snapshot is not their carrier, it only duplicates them onto disk.
"""

import subprocess

import pytest

# Imported inside the tests on purpose: this file must stay collectable on a tree where the
# exclusion does not exist yet, so the "red" run reports failing assertions (not a collection
# error that makes the before/after numbers incomparable).


# ---------------------------------------------------------------------------
# Unit: the pattern is name-shaped (credential suffixes), not an allowlist.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "BOCHA_API_KEY",
    "TUSHARE_TOKEN",
    "ALIYUN_ACCESS_KEY_SECRET",
    "ALIYUN_ACCESS_KEY_ID",
    "ZHIPU_API_KEY",
    "TINYFISH_API_KEY",
    "S2_API_KEY",
    "AWS_PRIVATE_KEY",
    "GITHUB_CLIENT_SECRET",
    "MY_PASSWORD",
    "SOME_PASSWD",
    "OPERATOR_SECRET",
    "CUSTOM_CREDENTIALS",
    "lower_case_token",
])
def test_pattern_matches_credential_names(name):
    from tools.environments.base_session_env import _SNAPSHOT_SECRET_NAME_RE

    assert _SNAPSHOT_SECRET_NAME_RE.search(name), name


@pytest.mark.parametrize("name", [
    "PATH",
    "HOME",
    "MYVAR",
    "HERMES_REDACT_SECRETS",       # a boolean flag, not a credential value
    "TERMINAL_DOCKER_SNAP_COMPAT",
    "TOKENIZER_PATH",              # suffix is not a credential suffix
    "API_KEYS_FILE",
])
def test_pattern_ignores_non_credential_names(name):
    from tools.environments.base_session_env import _SNAPSHOT_SECRET_NAME_RE

    assert not _SNAPSHOT_SECRET_NAME_RE.search(name), name


# ---------------------------------------------------------------------------
# Dump: secrets dropped, ordinary exports kept — including a key that only ever
# existed in the shell (never visible to Python), which the name sweep catches.
# ---------------------------------------------------------------------------

@pytest.mark.platforms("posix")  # POSIX bash snapshot path
def test_export_dump_drops_credential_names():
    from tools.environments.base_session_env import _export_dump_excluding_session_vars

    exports = "; ".join([
        'export BOCHA_API_KEY="sk-live-1234567890"',
        'export TUSHARE_TOKEN="tok-1234567890"',
        'export MY_PASSWORD="hunter2-1234567890"',
        'export HERMES_HOME="/h"',
        'export MYVAR="keep"',
    ])
    out = subprocess.run(
        ["bash", "-c", f"{exports}; {_export_dump_excluding_session_vars('/dev/stdout')}"],
        capture_output=True, text=True, check=True).stdout
    for name in ("BOCHA_API_KEY", "TUSHARE_TOKEN", "MY_PASSWORD"):
        assert f"declare -x {name}=" not in out, f"{name} persisted into the snapshot"
    assert 'declare -x HERMES_HOME="/h"' in out
    assert 'declare -x MYVAR="keep"' in out


# ---------------------------------------------------------------------------
# Integration: a real LocalEnvironment drops the secret from the snapshot file and
# keeps handing it to commands (the spawn env carries it), and a stale value left
# in an older snapshot can never override the fresh per-command one.
# ---------------------------------------------------------------------------

@pytest.mark.platforms("posix")  # POSIX bash snapshot path
def test_local_env_snapshot_has_no_secret_but_commands_still_get_it(tmp_path):
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30,
                           env={"PROBE_API_KEY": "probe-value-0123456789", "MYVAR": "keep"})
    try:
        first = env.execute('printf "[%s]" "$PROBE_API_KEY"')
        assert "[probe-value-0123456789]" in first.get("output", ""), first

        snap = env._snapshot_path
        with open(snap, encoding="utf-8", errors="replace") as f:
            text = f.read()
        assert "PROBE_API_KEY" not in text, "credential name persisted into the snapshot"
        assert "probe-value-0123456789" not in text
        assert 'declare -x MYVAR="keep"' in text, "ordinary exports must still persist"

        # Per-command env is the carrier: the next command still sees the key even though
        # the snapshot never does.
        second = env.execute('printf "[%s]" "$PROBE_API_KEY"')
        assert "[probe-value-0123456789]" in second.get("output", ""), second

        # A snapshot written before this exclusion existed (or by another profile) must not
        # re-export a stale key over the value this command was spawned with.
        with open(snap, "a", encoding="utf-8") as f:
            f.write('declare -x PROBE_API_KEY="stale-from-old-snapshot"\n')
        third = env.execute('printf "[%s]" "$PROBE_API_KEY"')
        assert "[probe-value-0123456789]" in third.get("output", ""), third

        # …and the re-dump after that command purged the stale line again.
        with open(snap, encoding="utf-8", errors="replace") as f:
            assert "stale-from-old-snapshot" not in f.read()
    finally:
        env.cleanup()


@pytest.mark.platforms("posix")  # POSIX bash snapshot path
def test_shell_only_exported_secret_does_not_persist(tmp_path):
    """A key exported inside a command (never in the harness process env) is swept by name."""
    from tools.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    try:
        env.execute('export AD_HOC_TOKEN="ad-hoc-value-0123456789"; :')
        with open(env._snapshot_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        assert "AD_HOC_TOKEN" not in text
        nxt = env.execute('printf "[%s]" "${AD_HOC_TOKEN:-unset}"')
        assert "[unset]" in nxt.get("output", ""), nxt
    finally:
        env.cleanup()
