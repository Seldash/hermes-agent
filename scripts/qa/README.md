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

## a11y 门禁的两个「态」必须显式声明

同一个 dashboard 在**空态**（全新 HERMES_HOME，首启横幅 + 空列表）与**常规态**（已有 profile/会话）下，axe 的违规集合并不相同 —— 所以 `axe_scan.py --state {empty,configured}` 是**必填**参数，不声明态就不给结论（旧的无参调用会直接 argparse 报错，这是故意的）。`--verify-state` 用于反查页面是否真处于声明的态，防止「拿空态读数冒充常规态」；**它只对 `/models` 页有效**：探针读该页 i18n `models.noModelsData` 文案（en: `No model usage data for this period`），匹配到判 `empty`、匹配不到判 `configured`。首启横幅与会话列表**不参与**判定。

退出码是**只对 `mismatch` 红**：`state_verified=mismatch` → exit 1；`state_verified=unknown`（非 `/models` 页，无态标记可读）→ **不红，exit 0**。因此 `GATE_URL` 一旦指向非 `/models` 页，`--verify-state` 会静默降级为 `unknown` 并照样给绿 —— 四个 `gate:*` 脚本默认都打 `http://127.0.0.1:8123/models`，不要为测别的页面去改这个默认值；测别的页面只能依赖 axe 读数，不能把态校验算进结论。

验收口径：两态各跑一次 `--undersized report --verify-state`，都要 `critical_serious=0`、`state_verified=ok`。**`ok` 是硬要求，`unknown` 不算通过**——它说明被测页不是 `/models`，态根本没验（尽管退出码为 0）。

## known-upstream 白名单（fail closed）

`a11y-whitelist.json` 只登记**三级依赖内的上游缺陷**，一条命中需五个条件同时成立：

1. 规则 id 一致；2. 该违规的**全部**节点都命中 `selector`（部分命中不豁免）；3. 当前态在 `states` 里；4. `recheck_by` 未过期；5. 自动探测到的 `@nous-research/ui` 版本等于 `upstream.version`（`--ui-version` 可显式覆盖）。

任一不成立都按原样报红 —— 过期（`expired`）与版本漂移（`version_mismatch`）都会让门禁 exit 1，提醒重新评估上游是否已修。当前仅一条：`stats.js:24` 硬编码 `opacity-20` 的装饰点线（1.73:1），复查日 2027-02-15。

## 24×24 判罚分工：axe 报、spacing_probe 判

`--undersized {fail,report,ignore}` 默认仍是 `fail`（不放宽旧行为）。门禁脚本显式传 `report`：undersized 照旧打印但不影响退出码，**SC 2.5.8 的间距例外由 `spacing_probe.py` 判**（可见且不满足例外的才 VIOLATION）。也就是说这两条不是二选一，`gate:a11y:both` 会把两态 axe + 间距一起跑。
