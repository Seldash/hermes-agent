# Web 前端质量门脚本（门 5 交互性能 / 门 6 可访问性 / 体积预算）

来源：2026-11-15 由 trantor 设备与入口运维域实测沉淀后迁入仓库，避免跨 profile 绝对路径引用。

| 脚本 | 作用 | 依赖 |
|---|---|---|
| `budget_check.sh <dist_dir> [总预算KB=700] [单块预算KB=250]` | 构建产物 gzip 体积预算（超限 exit 1） | bash + gzip，无 Python 依赖 |
| `interaction_probe.py <url> --cpu 4 --slow4g` | LCP/CLS/INP（PerformanceObserver + CDP 节流，阈值 LCP≤2500/INP≤200/CLS≤0.1） | `pip install playwright && playwright install chromium` |
| `axe_scan.py <url> [--tags …]` | axe-core 违规 + WCAG 2.2 目标尺寸 24×24 硬判定 | 同上；axe-core 默认取 jsDelivr，可 `--axe` 本地文件 |

用法示例（dashboard 需先 `cd web && npm run build`）：

```bash
HERMES_HOME=$(mktemp -d) HERMES_DASHBOARD_SESSION_TOKEN=e2e-token \
  python3 -m hermes_cli.main dashboard --port 8124 --skip-build --no-open &
python3 scripts/qa/interaction_probe.py http://127.0.0.1:8124/models --cpu 4 --slow4g --viewport 390x844
python3 scripts/qa/axe_scan.py http://127.0.0.1:8124/models
bash scripts/qa/budget_check.sh hermes_cli/web_dist
```

CI 尚未接线：仓库 CI 是 node 侧，跑这两条需在 runner 上装 Python playwright + 浏览器，属独立基建决定（不是本脚本的缺口）。

### 24×24 目标尺寸的间距例外（SC 2.5.8）

`axe_scan.py` 只做「尺寸不足」硬判定，不判间距例外——单看它会把合规元素报成待修。
`spacing_probe.py` 补这一层：以每个 <24×24 目标包围盒中心为圆心、半径 12px 作圆，
不与任何其他目标相交 ⇒ 落在间距例外内（EXEMPT-spacing）；嵌套在 ≥24×24 可交互祖先内
⇒ 真目标是祖先生效（EXEMPT-nested）；只有真相交才判 VIOLATION。

```bash
python3 scripts/qa/spacing_probe.py http://127.0.0.1:8124/models --viewport 390x844
```

必带视口过滤：移动端关闭态抽屉用 transform 平移到 x=-240，其按钮 rect 仍在 DOM 里，
不过滤会算出 `d=0` 相交假违规（实测 3 条）。同时按视口宽度分别测——1280 宽度下
顶栏品牌区（HERMES TEAL / EN）与 DOCUMENTATION 只距 4.6/5.8px，是真违规，390 宽度不出现。
