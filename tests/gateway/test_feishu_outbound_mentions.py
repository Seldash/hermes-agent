"""Outbound bot→bot real-mention tests for the Feishu adapter.

Regression for the coordination-group delivery failure (2026-08-22):
a bot's group reply that only contains plain-text "@Name" is NOT a real
Feishu mention, so the platform never pushes it to the named bot and the
receiver's ``FEISHU_ALLOW_BOTS=mentions`` admission would reject it even
if it arrived. ``FEISHU_BOT_MENTION_MAP`` names are upgraded to ``at``
post elements on send.
"""

import json

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_adapter = load_plugin_adapter("feishu")


def _bare():
    inst = object.__new__(_adapter.FeishuAdapter)
    inst._bot_mention_map = {
        "GameDev": "ou_1657test00000000000000000000",
        "Default Hermes": "ou_59d46test0000000000000000000",
    }
    return inst


def _extract(content: str):
    return _bare()._extract_mentions(content)


def _at_ids(payload_str: str) -> list[str]:
    payload = json.loads(payload_str)
    ids: list[str] = []
    for lang_val in payload.values():
        if not isinstance(lang_val, dict):
            continue
        for block in lang_val.get("content", []):
            for el in (block if isinstance(block, list) else [block]):
                if isinstance(el, dict) and el.get("tag") == "at":
                    ids.append(el.get("user_id", ""))
    return ids


def test_extract_basic():
    out, ids, names = _extract("@GameDev 请确认收到")
    assert out == "@_mention_0 请确认收到"
    assert ids == ["ou_1657test00000000000000000000"]
    assert names == ["GameDev"]


def test_extract_no_mention_unchanged():
    out, ids, names = _extract("无mention纯文本")
    assert out == "无mention纯文本"
    assert ids == [] and names == []


def test_extract_name_boundary_no_partial_match():
    # "@GameDevX" must NOT match the "GameDev" key (trailing letter).
    out, ids, _ = _extract("@GameDevX 不匹配后缀字母")
    assert ids == []
    assert "@_mention_" not in out


def test_extract_longest_name_wins():
    out, ids, names = _extract("前缀 @Default Hermes 后缀")
    assert out == "前缀 @_mention_0 后缀"
    assert names == ["Default Hermes"]


def test_extract_multiple_names():
    out, ids, names = _extract("同时 @GameDev 和 @Default Hermes")
    assert out == "同时 @_mention_0 和 @_mention_1"
    assert len(ids) == 2 and len(names) == 2


def test_extract_with_markdown():
    out, ids, _ = _extract("**加粗标题** @GameDev 内容")
    assert out == "**加粗标题** @_mention_0 内容"
    assert ids


def test_payload_at_element_inserted_between_md():
    inst = _bare()
    content, ids, _ = inst._extract_mentions("**标题** @GameDev 请确认")
    msg_type, payload = inst._build_outbound_payload(content, mention_ids=ids)
    assert msg_type == "post"
    rows = json.loads(payload)["zh_cn"]["content"]
    assert len(rows) == 1
    assert rows[0] == [
        {"tag": "md", "text": "**标题** "},
        {"tag": "at", "user_id": "ou_1657test00000000000000000000"},
        {"tag": "md", "text": " 请确认"},
    ]


def test_payload_forces_post_when_mention_present():
    inst = _bare()
    # Plain text with no markdown hint: mention presence must still force post.
    content, ids, _ = inst._extract_mentions("@GameDev 收到请回")
    msg_type, payload = inst._build_outbound_payload(content, mention_ids=ids)
    assert msg_type == "post"
    assert _at_ids(payload) == ["ou_1657test00000000000000000000"]


def test_payload_no_mention_backward_compatible():
    inst = _bare()
    content, ids, _ = inst._extract_mentions("普通 **文本** 无@")
    msg_type, payload = inst._build_outbound_payload(content, mention_ids=ids)
    assert msg_type == "post"
    assert _at_ids(payload) == []


def test_payload_code_fence_keeps_mention_row():
    inst = _bare()
    content, ids, _ = inst._extract_mentions("说明\n```\npython\nprint(1)\n```\n@GameDev 收尾")
    _, payload = inst._build_outbound_payload(content, mention_ids=ids)
    rows = json.loads(payload)["zh_cn"]["content"]
    assert rows[-1] == [
        {"tag": "at", "user_id": "ou_1657test00000000000000000000"},
        {"tag": "md", "text": " 收尾"},
    ]


def test_restore_mention_text_fallback():
    inst = _bare()
    content, ids, names = inst._extract_mentions("**标题** @GameDev 请确认")
    restored = inst._restore_mention_text(content, ids, names)
    assert "@_mention_" not in restored
    assert "@GameDev" in restored
