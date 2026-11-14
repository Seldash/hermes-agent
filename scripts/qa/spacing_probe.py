#!/usr/bin/env python3
"""WCAG 2.2 SC 2.5.8 目标尺寸（24x24）间距例外测量。

对每个 <24x24 的目标：
 1) 若存在 ≥24x24 的可交互祖先（真目标其实是祖先），判 EXEMPT-nested。
 2) 否则按 SC 2.5.8 Spacing 例外判定：以该目标包围盒中心为圆心、半径 12px 的圆，
    若不与任何其他目标的包围盒、也不与另一个 undersized 目标的圆相交 ⇒ EXEMPT-spacing。
 3) 相交 ⇒ VIOLATION（列出最近邻与距离）。

用法: python3 spacing_probe.py <url> [--viewport 390x844] [--json out.json]
"""
import argparse
import json
import sys
from playwright.sync_api import sync_playwright

PROBE = r"""
() => {
  const R = 12;                     // 24px 直径圆的半径
  const SEL = 'a[href], button, [role=button], input, select, [tabindex]:not([tabindex="-1"])';
  const VW = document.documentElement.clientWidth, VH = document.documentElement.clientHeight;
  // 视口过滤：关闭态抽屉（transform 平移到 x=-240）等元素 rect 仍在 DOM 里但用户不可达，
  // 不排除会把「侧栏按钮 vs 侧栏导航」算成相交（实测 d=0 假违规）。
  const inView = r => r.right > 0 && r.left < VW && r.bottom > 0 && r.top < VH;
  const vis = el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return null;
    if (!inView(r)) return null;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') return null;
    if (el.checkVisibility && !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return null;
    return {x: r.left, y: r.top, w: r.width, h: r.height};
  };
  const label = el => (el.innerText || el.value || el.getAttribute('aria-label') ||
                       el.getAttribute('title') || '').trim().replace(/\s+/g, ' ').slice(0, 32);
  const path = el => {
    const seg = [];
    let n = el;
    for (let i = 0; n && i < 3; i++, n = n.parentElement) {
      let s = n.tagName.toLowerCase();
      if (n.id) { s += '#' + n.id; seg.unshift(s); break; }
      const cls = (n.className || '').toString().trim().split(/\s+/).slice(0, 2).join('.');
      if (cls) s += '.' + cls;
      seg.unshift(s);
    }
    return seg.join(' > ');
  };
  const circleHitsRect = (cx, cy, r, box) => {
    const nx = Math.max(box.x, Math.min(cx, box.x + box.w));
    const ny = Math.max(box.y, Math.min(cy, box.y + box.h));
    const d = Math.hypot(cx - nx, cy - ny);
    return {hit: d < r, d};
  };
  const circleHitsCircle = (ax, ay, bx, by) => Math.hypot(ax - bx, ay - by) < 2 * R;

  const all = [];
  for (const el of document.querySelectorAll(SEL)) {
    const box = vis(el);
    if (!box) continue;
    all.push({el, box, tag: el.tagName.toLowerCase(), label: label(el), path: path(el)});
  }
  const out = [];
  for (const t of all) {
    if (t.box.w >= 24 && t.box.h >= 24) continue;
    const cx = t.box.x + t.box.w / 2, cy = t.box.y + t.box.h / 2;
    // 1) 嵌套在更大可交互祖先内 ⇒ 真目标是祖先
    let eff = null;
    for (let n = t.el.parentElement; n; n = n.parentElement) {
      if (!n.matches || !n.matches(SEL)) continue;
      const b = vis(n);
      if (b && b.w >= 24 && b.h >= 24) { eff = {tag: n.tagName.toLowerCase(), label: label(n),
        w: Math.round(b.w), h: Math.round(b.h), path: path(n)}; break; }
    }
    const rec = {tag: t.tag, w: Math.round(t.box.w), h: Math.round(t.box.h),
                 label: t.label, path: t.path, center: [Math.round(cx), Math.round(cy)]};
    if (eff) { rec.verdict = 'EXEMPT-nested'; rec.effective_target = eff; out.push(rec); continue; }
    // 2) 间距例外：与其他目标（排除自身及其祖先/后代）的相交检查
    let nearest = null, hitOn = null;
    for (const o of all) {
      if (o.el === t.el) continue;
      if (t.el.contains(o.el) || o.el.contains(t.el)) continue;
      const isSmall = o.box.w < 24 || o.box.h < 24;
      let hit, d;
      if (isSmall) {
        const ox = o.box.x + o.box.w / 2, oy = o.box.y + o.box.h / 2;
        d = Math.hypot(cx - ox, cy - oy);
        hit = circleHitsCircle(cx, cy, ox, oy);
      } else {
        const r = circleHitsRect(cx, cy, R, o.box);
        hit = r.hit; d = r.d;
      }
      if (!nearest || d < nearest.d) nearest = {d: Math.round(d * 10) / 10, tag: o.tag,
        label: o.label, small: isSmall};
      if (hit && !hitOn) hitOn = {tag: o.tag, label: o.label, small: isSmall, d: Math.round(d * 10) / 10};
    }
    rec.nearest = nearest;
    if (hitOn) { rec.verdict = 'VIOLATION'; rec.intersects = hitOn; }
    else { rec.verdict = 'EXEMPT-spacing'; }
    out.push(rec);
  }
  return out;
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--viewport", default="390x844")
    ap.add_argument("--browser", default="chromium")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    w, h = (int(x) for x in a.viewport.split("x"))
    with sync_playwright() as p:
        b = getattr(p, a.browser).launch()
        pg = b.new_page(viewport={"width": w, "height": h})
        pg.goto(a.url, wait_until="load")
        pg.wait_for_timeout(900)
        rows = pg.evaluate(PROBE)
        b.close()
    viol = [r for r in rows if r["verdict"] == "VIOLATION"]
    out = {"url": a.url, "viewport": f"{w}x{h}", "undersized": len(rows),
           "violations": len(viol), "rows": rows}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if a.json:
        open(a.json, "w").write(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if not viol else 1)


if __name__ == "__main__":
    main()
