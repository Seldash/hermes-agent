"""Body-growth block on skills that are nearly at the SKILL.md content cap.

Close to MAX_SKILL_CONTENT_CHARS the next sizeable lesson tips the file over the
limit, and a rejected write leaves no in-tool path but editing the file behind the
tool's back (no atomic replace, no security scan, no ledger). So writes that GROW a
SKILL.md with less than _floor() characters of headroom are refused
with a pointer to references/; shrinking and net-neutral writes stay allowed, and a
skill with headroom is unaffected.
"""

import json

import pytest

from tools.skill_manager_tool import MAX_SKILL_CONTENT_CHARS, skill_manage


@pytest.fixture(autouse=True)
def isolate_skills(tmp_path, monkeypatch):
    """Redirect SKILLS_DIR to a temp directory."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr("tools.skill_manager_tool.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills_dir)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return skills_dir


_HEADER = "---\nname: {name}\ndescription: A test skill\n---\n# Test Skill\n\n"


def _floor():
    """The growth floor, imported locally so the pre-patch tree reds tests, not collection."""
    from tools.skill_manager_tool import SKILL_BODY_HEADROOM_FLOOR
    return SKILL_BODY_HEADROOM_FLOOR


MARKER = 2_000


def _content(name: str, total_chars: int) -> str:
    """Valid SKILL.md content of exactly ``total_chars``, holding one unique ``U`` run.

    The run gives a patchable block: a wall of identical characters is not a unique
    match (every offset-length window matches), so shrinking tests need a marker.
    """
    header = _HEADER.format(name=name)
    filler = max(0, total_chars - len(header))
    mark = min(MARKER, filler)
    return header + "U" * mark + "x" * (filler - mark)


def _near_cap_content(name: str, headroom: int = 5_000) -> str:
    return _content(name, MAX_SKILL_CONTENT_CHARS - headroom)


class TestGuardUnit:
    """Pure-function behaviour of _skill_body_growth_guard."""

    @staticmethod
    def _guard(label, before, after):
        from tools.skill_manager_tool import _skill_body_growth_guard
        return _skill_body_growth_guard(label, before, after)

    def test_plenty_of_headroom_allows_growth(self):
        before = "y" * 10_000
        assert self._guard("SKILL.md", before, before + "z" * 5_000) is None


    def test_headroom_exactly_at_floor_allows_growth(self):
        before = "y" * (MAX_SKILL_CONTENT_CHARS - _floor())
        assert self._guard("SKILL.md", before, before + "z") is None


    def test_low_headroom_rejects_growth(self):
        before = "y" * (MAX_SKILL_CONTENT_CHARS - _floor() + 1)
        err = self._guard("SKILL.md", before, before + "z")
        assert err is not None
        assert "references/" in err


    def test_low_headroom_allows_shrink(self):
        before = "y" * (MAX_SKILL_CONTENT_CHARS - 1_000)
        assert self._guard("SKILL.md", before, before[:-10]) is None


    def test_low_headroom_allows_net_neutral(self):
        before = "y" * (MAX_SKILL_CONTENT_CHARS - 1_000)
        assert self._guard("SKILL.md", before, before) is None


    def test_unreadable_before_fails_open(self):
        assert self._guard("SKILL.md", "", "z" * 500) is None


class TestGuardThroughTool:
    """The block is wired into every SKILL.md write path (patch / write_file / edit)."""

    def test_patch_growth_refused_on_near_cap_skill(self, isolate_skills):
        near = _near_cap_content("near-cap")
        assert len(near) == MAX_SKILL_CONTENT_CHARS - 5_000
        assert json.loads(skill_manage(action="create", name="near-cap", content=near))["success"]

        result = json.loads(skill_manage(
            action="patch", name="near-cap",
            old_string="# Test Skill\n\n", new_string="# Test Skill\n\nNEW LESSON\n"))
        assert result["success"] is False
        assert "references/" in result["error"]
        # The rejected write must leave the file untouched.
        on_disk = (isolate_skills / "near-cap" / "SKILL.md").read_text(encoding="utf-8")
        assert on_disk == near


    def test_patch_shrink_allowed_on_near_cap_skill(self, isolate_skills):
        near = _near_cap_content("near-cap-shrink")
        json.loads(skill_manage(action="create", name="near-cap-shrink", content=near))

        result = json.loads(skill_manage(
            action="patch", name="near-cap-shrink",
            old_string="U" * MARKER, new_string="U" * 10))
        assert result["success"] is True


    def test_write_file_references_allowed_on_near_cap_skill(self, isolate_skills):
        near = _near_cap_content("near-cap-refs")
        json.loads(skill_manage(action="create", name="near-cap-refs", content=near))

        result = json.loads(skill_manage(
            action="write_file", name="near-cap-refs", file_path="references/lesson.md",
            file_content="# Lesson\n\n" + "detail " * 2_000))
        assert result["success"] is True


    def test_write_file_skill_md_growth_refused(self, isolate_skills):
        near = _near_cap_content("near-cap-wf")
        json.loads(skill_manage(action="create", name="near-cap-wf", content=near))

        result = json.loads(skill_manage(
            action="write_file", name="near-cap-wf", file_path="SKILL.md",
            file_content=near + "NEW LESSON\n"))
        assert result["success"] is False
        assert "references/" in result["error"]


    def test_full_rewrite_growth_refused(self, isolate_skills):
        near = _near_cap_content("near-cap-edit")
        json.loads(skill_manage(action="create", name="near-cap-edit", content=near))

        result = json.loads(skill_manage(
            action="patch", name="near-cap-edit", content=near + "NEW LESSON\n"))
        assert result["success"] is False
        assert "references/" in result["error"]


    def test_full_rewrite_shrink_allowed(self, isolate_skills):
        near = _near_cap_content("near-cap-edit-shrink")
        json.loads(skill_manage(action="create", name="near-cap-edit-shrink", content=near))

        shrunk = _content("near-cap-edit-shrink", 20_000)
        result = json.loads(skill_manage(
            action="patch", name="near-cap-edit-shrink", content=shrunk))
        assert result["success"] is True


    def test_small_skill_growth_unaffected(self, isolate_skills):
        small = _content("small-skill", 3_000)
        json.loads(skill_manage(action="create", name="small-skill", content=small))

        result = json.loads(skill_manage(
            action="patch", name="small-skill",
            old_string="# Test Skill\n\n", new_string="# Test Skill\n\n" + "lesson " * 2_000))
        assert result["success"] is True


    def test_new_skill_at_cap_is_not_blocked(self, isolate_skills):
        """create writes a target that does not exist yet — nothing to grow from."""
        at_cap = _content("at-cap-new", MAX_SKILL_CONTENT_CHARS - 100)
        result = json.loads(skill_manage(action="create", name="at-cap-new", content=at_cap))
        assert result["success"] is True


class TestFrontGate:
    """Front gate (wj 809235): a body past SKILL_BODY_REF_GATE_CHARS only takes patches.

    The 90K warning arrives after the point where the write path refuses all growth, so
    big bodies are steered to references/ while they still have headroom. Ordinary patches
    keep working; a legitimate large patch is lifted per skill by a bounded exemption.
    """

    @staticmethod
    def _guard(before, after, rel=None):
        from tools.skill_manager_tool import _skill_body_growth_guard
        return _skill_body_growth_guard("SKILL.md", before, after, rel=rel)

    @staticmethod
    def _exempt(tmp_path, rel, **entry):
        import json as _json
        path = tmp_path / "data" / "skill_body_gate_exempt.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps({"schema": 1, "exemptions": {rel: entry}},
                                    ensure_ascii=False), encoding="utf-8")
        return path

    def test_big_body_large_growth_refused(self):
        before = "y" * 21_000
        err = self._guard(before, before + "z" * 2_000, rel="devops/demo/SKILL.md")
        assert err is not None
        assert "front gate" in err and "references/" in err

    def test_big_body_small_patch_allowed(self):
        before = "y" * 21_000
        assert self._guard(before, before + "z" * 500) is None

    def test_big_body_net_neutral_allowed(self):
        before = "y" * 21_000
        assert self._guard(before, before) is None

    def test_body_just_under_gate_grows_freely(self):
        from tools.skill_manager_tool import SKILL_BODY_REF_GATE_CHARS
        before = "y" * (SKILL_BODY_REF_GATE_CHARS - 1)
        assert self._guard(before, before + "z" * 5_000) is None

    def test_kill_switch_restores_old_behaviour(self, monkeypatch):
        monkeypatch.setenv("SKILL_BODY_REF_GATE", "0")
        before = "y" * 21_000
        assert self._guard(before, before + "z" * 2_000) is None

    def test_exemption_lifts_gate_for_that_rel_and_is_logged(self, tmp_path):
        rel = "devops/demo/SKILL.md"
        ledger = self._exempt(tmp_path, rel, reason="整节重排", expires="2099-01-01", uses_left=2)
        before = "y" * 21_000
        assert self._guard(before, before + "z" * 2_000, rel=rel) is None
        log = tmp_path / "data" / "skill_body_gate_exempt_log.jsonl"
        lines = log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["rel"] == rel
        assert json.loads(ledger.read_text(encoding="utf-8"))["exemptions"][rel]["uses_left"] == 1
        # A different skill is not covered by that exemption.
        assert self._guard(before, before + "z" * 2_000, rel="devops/other/SKILL.md") is not None

    def test_exhausted_exemption_refused(self, tmp_path):
        rel = "devops/demo/SKILL.md"
        self._exempt(tmp_path, rel, reason="用过一次", expires="2099-01-01", uses_left=0)
        before = "y" * 21_000
        assert self._guard(before, before + "z" * 2_000, rel=rel) is not None

    def test_expired_exemption_refused(self, tmp_path):
        rel = "devops/demo/SKILL.md"
        self._exempt(tmp_path, rel, reason="过期了", expires="2020-01-01")
        before = "y" * 21_000
        assert self._guard(before, before + "z" * 2_000, rel=rel) is not None

    def test_max_growth_cap_enforced(self, tmp_path):
        rel = "devops/demo/SKILL.md"
        self._exempt(tmp_path, rel, reason="只允许小改", expires="2099-01-01", max_growth=1_000)
        before = "y" * 21_000
        assert self._guard(before, before + "z" * 900, rel=rel) is None
        assert self._guard(before, before + "z" * 1_500, rel=rel) is not None

    def test_broken_ledger_never_grants_a_pass(self, tmp_path):
        path = tmp_path / "data" / "skill_body_gate_exempt.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        before = "y" * 21_000
        assert self._guard(before, before + "z" * 2_000, rel="devops/demo/SKILL.md") is not None


class TestFrontGateThroughTool:
    """The gate is wired into the real write path, not just the pure function."""

    def test_patch_large_growth_refused_on_big_skill(self, isolate_skills):
        big = _content("big-skill", 21_000)
        json.loads(skill_manage(action="create", name="big-skill", content=big))

        result = json.loads(skill_manage(
            action="patch", name="big-skill",
            old_string="# Test Skill\n\n", new_string="# Test Skill\n\n" + "lesson " * 300))
        assert result["success"] is False
        assert "references/" in result["error"]
        on_disk = (isolate_skills / "big-skill" / "SKILL.md").read_text(encoding="utf-8")
        assert on_disk == big

    def test_patch_small_edit_allowed_on_big_skill(self, isolate_skills):
        big = _content("big-skill-ok", 21_000)
        json.loads(skill_manage(action="create", name="big-skill-ok", content=big))

        result = json.loads(skill_manage(
            action="patch", name="big-skill-ok",
            old_string="# Test Skill\n\n", new_string="# Test Skill\n\n一句判据\n"))
        assert result["success"] is True

    def test_references_write_still_allowed_on_big_skill(self, isolate_skills):
        big = _content("big-skill-refs", 21_000)
        json.loads(skill_manage(action="create", name="big-skill-refs", content=big))

        result = json.loads(skill_manage(
            action="write_file", name="big-skill-refs", file_path="references/lesson.md",
            file_content="# Lesson\n\n" + "detail " * 1_000))
        assert result["success"] is True

