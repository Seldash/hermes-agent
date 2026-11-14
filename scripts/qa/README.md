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
