"""Behavior contract for the cron.allow_agent_scheduling config gate.

``_resolve_cron_disabled_toolsets`` decides which toolsets a cron-spawned
agent must never receive. Historically ``cronjob`` was hard-denied there as
loop-prevention policy. The ``cron.allow_agent_scheduling`` gate (config.yaml,
default off) makes that denial opt-out-able:

  - gate off / absent: byte-exact current behavior — ``cronjob`` denied.
  - gate on: ``cronjob`` dropped from the base denylist; ``messaging`` and
    ``clarify`` (interactivity constraints) are ALWAYS denied regardless of
    the gate.
  - user-level ``agent.disabled_toolsets`` still layers on top, so a user who
    denies ``cronjob`` globally keeps it denied even with the gate on
    (per-job enabled_toolsets can never widen past the config denylist).
"""

import pytest

from cron.scheduler import _resolve_cron_disabled_toolsets


# The toolsets that must be denied in cron context no matter what the
# agent-scheduling gate says: messaging/clarify are interactive-only.
# ``memory`` is NOT listed here — it is denied by the local override
# unconditionally (see ``test_memory_denied_by_local_override``), not by
# the gate itself.
ALWAYS_DISABLED = ["messaging", "clarify"]


class TestGateOffDefault:
    def test_empty_config_denies_cronjob(self):
        # Local override (patch 0002, CUSTOM_PATCHES.md, 2026-08-23): cron
        # agents get no ``memory`` toolset, so the expected denylist carries it.
        assert _resolve_cron_disabled_toolsets({}) == [
            "cronjob", "messaging", "clarify", "memory",
        ]




    @pytest.mark.parametrize("falsy", [False, None, "", 0])
    def test_falsy_values_keep_gate_off(self, falsy):
        cfg = {"cron": {"allow_agent_scheduling": falsy}}
        disabled = _resolve_cron_disabled_toolsets(cfg)
        assert "cronjob" in disabled


class TestGateOn:
    def test_cronjob_dropped_from_denylist(self):
        cfg = {"cron": {"allow_agent_scheduling": True}}
        disabled = _resolve_cron_disabled_toolsets(cfg)
        assert "cronjob" not in disabled

    def test_interactivity_denials_survive_the_gate(self):
        cfg = {"cron": {"allow_agent_scheduling": True}}
        disabled = _resolve_cron_disabled_toolsets(cfg)
        for name in ALWAYS_DISABLED:
            assert name in disabled

    def test_memory_denied_by_local_override(self):
        # Local override (patch 0002, CUSTOM_PATCHES.md, 2026-08-23): cron
        # agents run with skip_memory=True, so the ``memory`` toolset is
        # policy-denied regardless of the agent-scheduling gate.
        for cfg in ({}, {"cron": {"allow_agent_scheduling": True}}):
            assert "memory" in _resolve_cron_disabled_toolsets(cfg)

    def test_user_denylist_wins_over_gate(self):
        # A user who denies cronjob in agent.disabled_toolsets keeps it
        # denied even with the gate on — the gate only removes the built-in
        # policy denial, never the user's own config denylist.
        cfg = {
            "cron": {"allow_agent_scheduling": True},
            "agent": {"disabled_toolsets": ["cronjob"]},
        }
        assert "cronjob" in _resolve_cron_disabled_toolsets(cfg)

    def test_unrelated_user_denylist_layers_without_reviving_cronjob(self):
        cfg = {
            "cron": {"allow_agent_scheduling": True},
            "agent": {"disabled_toolsets": ["browser"]},
        }
        disabled = _resolve_cron_disabled_toolsets(cfg)
        assert "browser" in disabled
        assert "cronjob" not in disabled


class TestUserLayerUnchanged:
    def test_user_denylist_still_layers_when_gate_off(self):
        cfg = {"agent": {"disabled_toolsets": ["browser", "cronjob"]}}
        disabled = _resolve_cron_disabled_toolsets(cfg)
        assert "browser" in disabled
        # No duplicate when the user names an already-denied toolset.
        assert disabled.count("cronjob") == 1


    def test_blank_and_whitespace_entries_ignored(self):
        cfg = {
            "cron": {"allow_agent_scheduling": True},
            "agent": {"disabled_toolsets": ["", "  ", "browser"]},
        }
        disabled = _resolve_cron_disabled_toolsets(cfg)
        assert "browser" in disabled
        assert "" not in disabled
