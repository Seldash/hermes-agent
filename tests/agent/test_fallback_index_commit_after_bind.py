"""Regression tests for 712843 / P31 — the fallback cursor commits only once a candidate binds.

``_fallback_index`` used to advance *before* the skip verdict and the route binding ran, so a
rejected candidate (skip verdict, "provider not configured", activation exception) burned its
own slot permanently. Once the last slot was burned the chain looked exhausted from the
outside: no further activation attempt ran — not even the "trying fallback..." status — even
after the transient reason (credential-pool cooldown, temporary entitlement rejection) cleared.

2026-10-15: four unpinned cron jobs hit HTTP 200 + 0 chunk + 600 s stale-stream three times,
had three configured backups each, and logged zero fallback attempts before terminating.

The invariant under test: ``_fallback_index`` moves only on a successful swap, and a walk that
rejects every remaining candidate leaves the cursor where it was so the next attempt can retry.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_agent(fallback_model):
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _mock_client(base_url="https://api.example.com/v1", api_key="fb-key"):
    mock = MagicMock()
    mock.base_url = base_url
    mock.api_key = api_key
    return mock


def test_skip_rejected_candidate_keeps_its_slot():
    """A candidate rejected by the skip verdict must stay available to a later attempt."""
    agent = _make_agent([{"provider": "openai", "model": "gpt-4o"}])
    verdicts = iter([True, False])  # first walk rejects it, second walk accepts it
    with (
        patch(
            "agent.chat_completion_helpers._should_skip_fallback_candidate",
            side_effect=lambda *args, **kwargs: next(verdicts),
        ),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_client(), "gpt-4o"),
        ),
    ):
        assert agent._try_activate_fallback() is False
        assert agent._fallback_index == 0
        assert agent._has_pending_fallback() is True
        assert agent._try_activate_fallback() is True
        assert agent._fallback_index == 1
    assert agent.model == "gpt-4o"
    assert agent.provider == "openai"


def test_activation_exception_keeps_its_slot():
    """A failed swap (bind raised) must not consume the candidate it never bound."""
    agent = _make_agent([{"provider": "openai", "model": "gpt-4o"}])
    calls = {"n": 0}

    def _resolve(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("client construction blew up")
        return _mock_client(), "gpt-4o"

    with patch("agent.auxiliary_client.resolve_provider_client", side_effect=_resolve):
        assert agent._try_activate_fallback() is False
        assert agent._fallback_index == 0
        assert agent._try_activate_fallback() is True
        assert agent._fallback_index == 1
    assert agent.model == "gpt-4o"


def test_unconfigured_entry_does_not_stop_the_walk():
    """An unconfigured entry is rejected and the walk continues; the cursor lands past the
    entry that actually bound, not past the rejected one."""
    chain = [
        {"provider": "openai", "model": "gpt-4o"},
        {"provider": "zai", "model": "glm-4.7"},
    ]
    agent = _make_agent(chain)
    resolved = iter([(None, "gpt-4o"), (_mock_client(), "glm-4.7")])
    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        side_effect=lambda *args, **kwargs: next(resolved),
    ):
        assert agent._try_activate_fallback() is True
        assert agent._fallback_index == 2
    assert agent.provider == "zai"
    # The unconfigured entry is memoized as unavailable: the next walk is a no-op that keeps
    # the committed cursor instead of replaying the failed binding.
    assert agent._try_activate_fallback() is False
    assert agent._fallback_index == 2


def test_all_candidates_rejected_keeps_cursor_and_says_why():
    """Rejecting every remaining candidate leaves the cursor untouched (retryable later) and
    surfaces the rejection ledger instead of silently blaming the primary provider."""
    chain = [
        {"provider": "openai", "model": "gpt-4o"},
        {"provider": "zai", "model": "glm-4.7"},
    ]
    agent = _make_agent(chain)
    statuses: list[str] = []
    agent._buffer_diagnostic_status = statuses.append
    with patch(
        "agent.chat_completion_helpers._should_skip_fallback_candidate",
        return_value=True,
    ):
        assert agent._try_activate_fallback() is False
        assert agent._fallback_index == 0
        assert agent._has_pending_fallback() is True
        # A later attempt re-walks them rather than reporting "nothing to fall back to".
        assert agent._try_activate_fallback() is False
        assert agent._fallback_index == 0
    assert len(agent._fallback_rejections) == 2
    assert any("全部不可用" in status for status in statuses)
