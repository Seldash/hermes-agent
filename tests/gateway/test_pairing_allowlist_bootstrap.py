"""Load-time pairing bootstrap: a configured allowlist must never lock its owner out.

Regression anchor: 2026-10-29 trantor — FEISHU_ALLOWED_USERS was moved out of the
profile .env while the pairing approved store was empty, so the owner was treated as
a stranger and had to be re-approved by hand from an old ``.env.bak``.
"""

import json
import logging
from contextlib import ExitStack
from unittest.mock import patch

from gateway.pairing import PairingStore

OWNER = "ou_16379877802845b72239318505afa875"
STRANGER = "ou_stranger_not_allowlisted"


def _make_store(tmp_path, allowlists, *, bootstrap=True):
    """Store rooted in tmp_path, allowlists keyed by env var name (e.g. FEISHU_...)."""
    home = tmp_path / "home"
    with ExitStack() as stack:
        stack.enter_context(patch("gateway.pairing.PAIRING_DIR", tmp_path))
        stack.enter_context(patch("gateway.pairing.get_hermes_home", return_value=home))
        stack.enter_context(
            patch("gateway.pairing._read_allowlist_env", side_effect=lambda env: allowlists.get(env, ""))
        )
        store = PairingStore(bootstrap_allowlist=bootstrap)
    return store


def _approved(tmp_path, platform="feishu"):
    path = tmp_path / f"{platform}-approved.json"
    return json.loads(path.read_text()) if path.exists() else {}


def test_empty_store_seeds_from_allowlist_and_warns(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="gateway.pairing"):
        store = _make_store(tmp_path, {"FEISHU_ALLOWED_USERS": OWNER})

    approved = _approved(tmp_path)
    assert list(approved) == [OWNER]
    assert approved[OWNER]["migrated_from_env"] is True
    assert approved[OWNER]["source_env"] == "FEISHU_ALLOWED_USERS"
    # The gate must now admit the allowlisted owner with an empty-looking store history.
    assert store.is_approved("feishu", OWNER) is True
    assert "Pairing allowlist bootstrap" in caplog.text


def test_bootstrap_does_not_admit_non_allowlisted_users(tmp_path):
    store = _make_store(tmp_path, {"FEISHU_ALLOWED_USERS": OWNER})

    assert store.is_approved("feishu", OWNER) is True
    assert store.is_approved("feishu", STRANGER) is False
    assert STRANGER not in _approved(tmp_path)
    # Never a wildcard: an allowlist of one must not become "everyone".
    assert "*" not in _approved(tmp_path)


def test_bootstrap_does_not_touch_store_when_allowlist_unset(tmp_path):
    store = _make_store(tmp_path, {})

    assert _approved(tmp_path) == {}
    assert store.is_approved("feishu", OWNER) is False


def test_seeded_entries_are_pruned_when_allowlist_drops_them(tmp_path):
    _make_store(tmp_path, {"FEISHU_ALLOWED_USERS": f"{OWNER},{STRANGER}-2"})
    assert set(_approved(tmp_path)) == {OWNER, f"{STRANGER}-2"}

    # Operator removes one id from the allowlist: the migrated copy must not survive,
    # otherwise de-authorization silently keeps working (union gate).
    store = _make_store(tmp_path, {"FEISHU_ALLOWED_USERS": OWNER})
    assert set(_approved(tmp_path)) == {OWNER}
    assert store.is_approved("feishu", f"{STRANGER}-2") is False


def test_manual_approval_survives_allowlist_prune(tmp_path):
    _make_store(tmp_path, {"FEISHU_ALLOWED_USERS": OWNER})

    home = tmp_path / "home"
    with patch("gateway.pairing.PAIRING_DIR", tmp_path), patch(
        "gateway.pairing.get_hermes_home", return_value=home
    ):
        store = PairingStore()
        store._approve_user("feishu", STRANGER, "manual")
    assert store.is_approved("feishu", STRANGER) is True

    _make_store(tmp_path, {"FEISHU_ALLOWED_USERS": OWNER})
    assert store.is_approved("feishu", STRANGER) is True
    assert _approved(tmp_path)[STRANGER].get("migrated_from_env") is None
