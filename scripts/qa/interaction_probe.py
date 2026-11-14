#!/usr/bin/env python3
"""交互性能探针：原生 PerformanceObserver 量 LCP / CLS / INP，带 CPU 与网络节流。

为什么不用 Lighthouse：Lighthouse 只给 TBT 作为 INP 的实验室代理，不产出 INP。
本脚本用 Event Timing API 直接取「有 interactionId 的事件里的最大 duration」，口径对齐
web.dev 的 INP 定义（未做 1/50 剔除，单页面手工流程里足够）。

用法：
  python3 interaction_probe.py <url> [--click ".sel1,.sel2"] [--viewport 390x844]
      [--cpu 4] [--slow4g] [--budget 200] [--json out.json]

退出码：0 = 全部在阈值内；3 = 有指标超阈值。
阈值默认：LCP 2500ms / INP 200ms / CLS 0.1（web.dev Core Web Vitals）。
"""
import argparse
import json
import sys
from playwright.sync_api import sync_playwright

INIT = r"""
window.__mw = { inp: 0, lcp: 0, cls: 0, events: [] };
try {
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) {
      if (e.interactionId) {
        if (e.duration > window.__mw.inp) window.__mw.inp = e.duration;
        window.__mw.events.push({ name: e.name, dur: e.duration, id: e.interactionId });
      }
    }
  }).observe({ type: 'event', buffered: true, durationThreshold: 16 });
} catch (err) { window.__mw.err = String(err); }
try {
  new PerformanceObserver((list) => {
    const es = list.getEntries();
    const last = es[es.length - 1];
    if (last) window.__mw.lcp = last.startTime + (last.loadTime || 0);
  }).observe({ type: 'largest-contentful-paint', buffered: true });
} catch (err) {}
try {
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) if (!e.hadRecentInput) window.__mw.cls += e.value;
  }).observe({ type: 'layout-shift', buffered: true });
} catch (err) {}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--click", default="", help="逗号分隔的选择器，依次点击以产生交互")
    ap.add_argument("--viewport", default="1280x800")
    ap.add_argument("--cpu", type=int, default=1, help="CPU 节流倍数，移动端典型 4")
    ap.add_argument("--slow4g", action="store_true")
    ap.add_argument("--budget", type=int, default=200, help="INP 阈值 ms")
    ap.add_argument("--browser", default="chromium", help="chromium|webkit|firefox（验 iOS 用 webkit）")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    w, h = (int(x) for x in a.viewport.split("x"))

    with sync_playwright() as p:
        b = getattr(p, a.browser).launch()
        ctx = b.new_context(viewport={"width": w, "height": h})
        ctx.add_init_script(INIT)
        pg = ctx.new_page()
        throttled = False
        if a.cpu > 1 or a.slow4g:
            if a.browser != "chromium":
                # CDP 只存在于 Chromium；WebKit/Firefox 没有 CPU/网络节流 API。
                # 宁可标明未节流，也不要静默给出不可比的数字。
                print(f"[warn] {a.browser} 不支持 CDP 节流，本次不节流；移动端节流口径请用 --browser chromium", file=sys.stderr)
            else:
                cdp = ctx.new_cdp_session(pg)
                if a.cpu > 1:
                    cdp.send("Emulation.setCPUThrottlingRate", {"rate": a.cpu})
                if a.slow4g:
                    cdp.send("Network.emulateNetworkConditions", {
                        "offline": False, "latency": 150,
                        "downloadThroughput": 1.6 * 1024 * 1024 / 8,
                        "uploadThroughput": 750 * 1024 / 8})
                throttled = True
        pg.goto(a.url, wait_until="load")
        pg.wait_for_timeout(1200)
        clicked = []
        for sel in [s.strip() for s in a.click.split(",") if s.strip()]:
            try:
                pg.locator(sel).first.click(timeout=5000)
                pg.wait_for_timeout(300)
                clicked.append(sel)
            except Exception:
                clicked.append(f"{sel}:MISS")
        pg.wait_for_timeout(800)
        pg.mouse.move(-1, -1)
        m = pg.evaluate("window.__mw")
        b.close()

    out = {
        "url": a.url, "viewport": f"{w}x{h}", "browser": a.browser, "cpu": a.cpu, "slow4g": a.slow4g,
        "throttled": throttled,
        "lcp_ms": round(m.get("lcp", 0)), "inp_ms": round(m.get("inp", 0)),
        "cls": round(m.get("cls", 0), 4), "interactions": len(m.get("events", [])),
        "clicked": clicked,
    }
    out["pass"] = out["lcp_ms"] <= 2500 and out["inp_ms"] <= a.budget and out["cls"] <= 0.1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if a.json:
        open(a.json, "w").write(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if out["pass"] else 3)


if __name__ == "__main__":
    main()
