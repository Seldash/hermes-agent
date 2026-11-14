#!/usr/bin/env python3
"""axe-core 可访问性扫描 + WCAG 2.2 目标尺寸(24x24)断言。

用法：
  python3 axe_scan.py <url> [--viewport 390x844] [--tags wcag2a,wcag2aa,wcag22aa]
      [--axe /path/to/axe.min.js] [--json out.json]

axe 脚本来源：优先 --axe 本地文件，否则取 jsDelivr 上的 axe-core@4.14.0。
目标尺寸按 WCAG 2.2 SC 2.5.8（至少 24x24 CSS px，或与相邻目标有足够间距）判定，
只做「尺寸不足」硬判定，不做间距例外推断——报告里给人复核。

退出码：0 = 无 critical/serious 且无 undersized；1 = 有。
"""
import argparse
import json
import sys
import urllib.request
from playwright.sync_api import sync_playwright

CDN = "https://cdn.jsdelivr.net/npm/axe-core@4.14.0/axe.min.js"

AXE_RUN = """
async ([tags]) => {
  const r = await axe.run(document, { runOnly: { type: 'tag', values: tags } });
  return r.violations.map(v => ({
    id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length,
    sample: (v.nodes[0] && v.nodes[0].target || []).join(' ')
  }));
}
"""

UNDERSIZED = """
() => {
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--viewport", default="390x844")
    ap.add_argument("--tags", default="wcag2a,wcag2aa,wcag22aa")
    ap.add_argument("--axe", default="")
    ap.add_argument("--browser", default="chromium", help="chromium|webkit|firefox")
    ap.add_argument("--json", default="")
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
        small = pg.evaluate(UNDERSIZED)
        b.close()

    bad = [v for v in violations if v["impact"] in ("critical", "serious")]
    out = {
        "url": a.url, "viewport": f"{w}x{h}", "browser": a.browser, "tags": tags,
        "violations_total": len(violations),
        "critical_serious": len(bad),
        "violations": violations,
        "undersized_targets": small,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if a.json:
        open(a.json, "w").write(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if not bad and not small else 1)


if __name__ == "__main__":
    main()
