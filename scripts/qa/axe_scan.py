#!/usr/bin/env python3
"""axe-core 可访问性扫描 + WCAG 2.2 目标尺寸(24x24)断言 + 双态声明 + known-upstream 白名单。

用法：
  python3 axe_scan.py <url> --state {empty,configured} [--viewport 390x844]
      [--tags wcag2a,wcag2aa,wcag22aa] [--axe /path/to/axe.min.js]
      [--whitelist conf.json] [--ui-version 0.18.2] [--json out.json]

态（必填，写进输出字段）：
  empty      = 全新 HERMES_HOME（无 provider 配置、无会话用量），/models 无模型卡片
  configured = 已配置 profile（有会话用量或 provider 配置），/models 有模型卡片
  门禁必须显式声明测哪一种，否则 CI 会把「态差异」当成回归，时红时绿。

白名单（known-upstream）：
  默认读同目录 a11y-whitelist.json。命中条件必须全部成立才豁免：
  规则 id 相同 + 该违规**所有**节点都命中 selector（部分命中不放行）+ 态在 entry.states 内
  + 未过 recheck_by + 上游包版本与 entry.upstream.version 一致。
  任一不成立即按原样报红（fail closed）。过期与版本漂移单独列在输出的
  whitelist_expired / whitelist_version_mismatch 里，不静默吞掉。

axe 脚本来源：优先 --axe 本地文件，否则取 jsDelivr 上的 axe-core@4.14.0。
退出码：0 = 无未豁免 critical/serious 且无 undersized；1 = 有。
"""
import argparse
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

CDN = "https://cdn.jsdelivr.net/npm/axe-core@4.14.0/axe.min.js"

HERE = Path(__file__).resolve().parent
DEFAULT_WHITELIST = HERE / "a11y-whitelist.json"
def _find_ui_pkg(start: Path) -> Path:
    """向上找 node_modules/@nous-research/ui/package.json；脚本被拷到侧路也能定位。"""
    rel = Path("node_modules") / "@nous-research" / "ui" / "package.json"
    for base in [start, *start.parents]:
        cand = base / rel
        if cand.is_file():
            return cand
    return start / rel


UI_PKG = _find_ui_pkg(HERE)

STATE_NOTE = {
    "empty": "全新 HERMES_HOME：无 provider 配置、无会话用量",
    "configured": "已配置 profile：有会话用量或 provider 配置",
}

AXE_RUN = """
async ([tags]) => {
  const r = await axe.run(document, { runOnly: { type: 'tag', values: tags } });
  return r.violations.map(v => ({
    id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length,
    targets: v.nodes.map(n => (n.target || []).join(' ')),
    sample: (v.nodes[0] && v.nodes[0].target || []).join(' ')
  }));
}
"""

UNDERSIZED = """() => {
  const out = [];
  const nodes = document.querySelectorAll('a[href], button, [role=button], input, select, [tabindex]:not([tabindex="-1"])');
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    if (r.width < 24 || r.height < 24) {
      out.push({ tag: el.tagName.toLowerCase(), w: Math.round(r.width), h: Math.round(r.height),
                 text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 24) });
    }
  }
  return out.slice(0, 40);
}
"""

STATE_PROBE = """() => {
  // 只认 /models 的空态文案（i18n 的 models.noModelsData，en: "No model usage data for this period"）。
  const t = (document.body && document.body.innerText) || '';
  const emptyMarker = /No model usage data|無模型|モデル使用|모델 사용/i.test(t);
  const modelsCard = /MODELS?\\b/i.test(t);
  return { emptyMarker, onModelsPage: /\\/models/.test(location.pathname), modelsCard };
}
"""


def _norm(s: str) -> str:
    """axe 的 target 里 CSS 特殊字符带反斜杠转义；白名单按去转义后的字面量写。"""
    return (s or "").replace("\\", "")


def _ui_version(path: Path = UI_PKG) -> str:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("version", "")
    except Exception:
        return ""


def _load_whitelist(path: Path):
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        print(f"[warn] 白名单不是合法 JSON，本次不豁免任何项: {exc}", file=sys.stderr)
        return []
    entries = data.get("entries", []) if isinstance(data, dict) else []
    return [e for e in entries if isinstance(e, dict)]


def _verdict(entry, v, state, today, ui_ver):
    """-> 'waived' | 'expired' | 'version_mismatch' | None（None = 不适用）"""
    if entry.get("rule") != v["id"]:
        return None
    selector = _norm(entry.get("selector", ""))
    targets = [_norm(t) for t in v.get("targets", [])]
    if not selector or not targets or not all(selector in t for t in targets):
        return None  # 部分命中不放行：宁红不漏
    states = entry.get("states") or []
    if states and state not in states:
        return None
    try:
        if today > date.fromisoformat(str(entry.get("recheck_by", ""))):
            return "expired"
    except ValueError:
        return "expired"  # 缺/坏复查日期按过期处理
    pinned = (entry.get("upstream") or {}).get("version") or ""
    if pinned and ui_ver and pinned != ui_ver:
        return "version_mismatch"
    return "waived"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--state", choices=sorted(STATE_NOTE), required=True,
                    help="被测态：empty=全新 HERMES_HOME；configured=已配置 profile（必填）")
    ap.add_argument("--viewport", default="390x844")
    ap.add_argument("--tags", default="wcag2a,wcag2aa,wcag22aa")
    ap.add_argument("--axe", default="")
    ap.add_argument("--browser", default="chromium", help="chromium|webkit|firefox")
    ap.add_argument("--whitelist", default=str(DEFAULT_WHITELIST))
    ap.add_argument("--undersized", choices=["fail", "report", "ignore"], default="fail",
                    help="24×24 硬判定如何影响退出码：fail=有线即红（默认，保持旧行为）；"
                         "report=只列不判（与 spacing_probe.py 的 SC 2.5.8 例外裁决分工时用）；"
                         "ignore=不检测")
    ap.add_argument("--ui-version", default="", help="覆盖自动探测的 @nous-research/ui 版本")
    ap.add_argument("--json", default="")
    ap.add_argument("--verify-state", action="store_true",
                    help="用页面标记反查声明态是否属实（/models 的 noModelsData 文案）："
                         "声明与页面不符时判定 state_mismatch 并红；找不到标记则记 unknown 不判")
    a = ap.parse_args()
    w, h = (int(x) for x in a.viewport.split("x"))

    if a.axe:
        src = open(a.axe, encoding="utf-8").read()
    else:
        src = urllib.request.urlopen(CDN, timeout=30).read().decode()

    tags = [t.strip() for t in a.tags.split(",") if t.strip()]
    with sync_playwright() as p:
        b = getattr(p, a.browser).launch()
        pg = b.new_page(viewport={"width": w, "height": h})
        pg.goto(a.url, wait_until="load")
        pg.wait_for_timeout(800)
        pg.add_script_tag(content=src)
        violations = pg.evaluate(AXE_RUN, [tags])
        small = pg.evaluate(UNDERSIZED) if a.undersized != "ignore" else []
        state_obs = pg.evaluate(STATE_PROBE) if a.verify_state else None
        b.close()

    state_verdict, state_seen = "not_checked", ""
    if a.verify_state:
        if not state_obs or not state_obs.get("onModelsPage"):
            state_verdict, state_seen = "unknown", "非 /models 页，无态标记可读"
        else:
            empty_seen = bool(state_obs.get("emptyMarker"))
            state_seen = "empty" if empty_seen else "configured"
            state_verdict = "ok" if state_seen == a.state else "mismatch"

    ui_ver = a.ui_version or _ui_version()
    entries = _load_whitelist(Path(a.whitelist) if a.whitelist else None)
    today = date.today()

    bad, waived, expired, drifted = [], [], [], []
    for v in violations:
        if v["impact"] not in ("critical", "serious"):
            continue
        hits = [e for e in entries if _verdict(e, v, a.state, today, ui_ver)]
        verdict = None
        entry = None
        for e in hits:
            if _verdict(e, v, a.state, today, ui_ver) == "waived":
                verdict, entry = "waived", e
                break
        if verdict != "waived" and hits:
            entry = hits[0]
            verdict = _verdict(entry, v, a.state, today, ui_ver)
        if verdict == "waived":
            wa = {
                "id": v["id"], "impact": v["impact"], "nodes": v["nodes"],
                "upstream": (entry.get("upstream") or {}),
                "recheck_by": entry.get("recheck_by"),
                "tracking": entry.get("tracking"),
            }
            waived.append(wa)
            print(f"[waived] {v['id']} {v['nodes']} node(s) ← known-upstream "
                  f"{wa['upstream'].get('package','?')}@{wa['upstream'].get('version','?')} "
                  f"(recheck {wa['recheck_by']})", file=sys.stderr)
        else:
            bad.append(v)
            if verdict == "expired":
                expired.append({"id": v["id"], "selector": entry.get("selector"),
                                "recheck_by": entry.get("recheck_by")})
            elif verdict == "version_mismatch":
                drifted.append({"id": v["id"], "expected": (entry.get("upstream") or {}).get("version"),
                                "installed": ui_ver})

    out = {
        "url": a.url, "viewport": f"{w}x{h}", "browser": a.browser, "tags": tags,
        "state": a.state, "state_note": STATE_NOTE[a.state],
        "state_verified": state_verdict, "state_seen": state_seen,
        "undersized_mode": a.undersized,
        "ui_version": ui_ver,
        "whitelist_file": str(a.whitelist) if entries else "",
        "whitelist_entries": len(entries),
        "violations_total": len(violations),
        "critical_serious": len(bad),
        "waived": waived,
        "whitelist_expired": expired,
        "whitelist_version_mismatch": drifted,
        "violations": violations,
        "undersized_targets": small,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if a.json:
        open(a.json, "w").write(json.dumps(out, ensure_ascii=False, indent=2))
    hard_size = a.undersized == "fail" and bool(small)
    sys.exit(0 if not bad and not hard_size and state_verdict != "mismatch" else 1)


if __name__ == "__main__":
    main()
