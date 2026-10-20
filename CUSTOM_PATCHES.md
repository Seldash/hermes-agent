# Hermes 本地定制登记（升级后逐条重放）

> 本文件登记 hermes-agent 源码树之外的本地定制与手动安装依赖，
> 每次 `hermes update`（uv sync 重建 venv）后逐条核对重放。
>
> 重放纪律（2026-08-25 事故教训）：08-24 `hermes update` 执行 `git reset --hard origin/main`
> 清除了 08-21/08-23 的本地提交（c1bd8df81 等），且 CUSTOM_PATCHES.md 被升级流程重建为模板
> 导致登记丢失 → asyncio 竞态补丁未重放 → 08-25 05:00:39 跨 loop RuntimeError 再现，default
> 入站黑洞 2h。**每次升级后必须 `git log --oneline -10` 核对本地提交是否仍在 + 逐条重放本表**。
>
> 自动化防线（2026-08-25 起）：升级后 `bash ~/.hermes/scripts/verify_patches.sh --full --strict --verbose`
> 全过才算完成；每日 cron「系统·补丁重放看门狗(08:40)」兜底（PASS 静默 / FAIL 推送）。
> ⚠️ 本文件在 git 树内会被升级重置，不可作唯一防线——验证器在 ~/.hermes/scripts/ 不受影响。

## 分支模型与升级路径（2026-09-10 起，勿改回）

| 项 | 值 | 作用 |
|---|---|---|
| 日常检出分支 | **`local-patches`**（= 本表 8 条补丁提交） | 让 `update_cmd.py:8302` 守卫生效 → 走 in-place 路径 |
| `main` | = `origin/main` 干净镜像 | 彻底消除 reset 触发条件（ff-only 不再失败） |
| config | `updates.parked_branch_strategy: update_in_place` | merge 冲突 → `merge --abort`，非破坏；**代价：不会自动升级成功** |
| 锚点 tag | `local-patches-base-20260910`（=54912371） | 回滚锚 |
| 补丁留底 | `~/.hermes/patches/20260910/`（`git format-patch origin/main..HEAD` 全量） | git 之外第二份 |
| NAS | `nas-hot-backup.sh` §5 每日 04:00 同步整个 hermes-agent **含 .git** | 第三份（tag/branch 都在；注意此段不在 DIRS 列表里，别误判"源码树未备份"） |

**根因**：8 条补丁长期承载在 `main` 上 → 裸跑 `hermes update` 时 8302 守卫跳过（只在 current≠main 生效）→ 8685 `merge --ff-only` 必失败 → **8752 `reset --hard origin/main` 静默丢 8 提交**（08-24 事故形态；v0.21.0 上游未修此路径）。

**顺序铁律**：`git tag 锚点` → `git switch -c local-patches` → `git branch -f main origin/main` → 最后才改 config。只有「在 local-patches **且** 配 update_in_place」是安全态；main 转干净镜像后，即使被切回 main 也无害（ff-only 成功，无 reset）。

**升级路径（不是 merge）**：`git switch -c <ver>-replay <release_tag>` → 逐条 `git show <sha> | git apply --3way` → `verify_patches.sh --full --strict` → `git branch -f local-patches HEAD`。
`update_in_place` 下 merge 只是**安全网**：补丁落在上游高频改写文件上，merge 必冲突 → `hermes update` 稳定「停住不破坏」（守住桌面按钮/cron/gateway 的无人值守触发面）。

## 手动 pip 包（uv sync 会清除，升级后必须重装）

| 包 | 用途 | 重装命令 |
|---|---|---|
| baostock | 因子数据源（expand_factor_db.py / rebuild_etf_cache.py / datafeed.py 等 10 脚本） | `~/.hermes/hermes-agent/venv/bin/pip install baostock` |
| pytest | hermes-agent 测试套件运行器（venv 重建后消失；proxy 单测等依赖） | `~/.hermes/hermes-agent/venv/bin/pip install pytest` |
| mcp 2.2.0（+ mcp-types 2.2.0） | 修 CVE-2026-59951（GHSA-84m7-p3x7-pcfv：StreamableHTTP 服务端 stateful 会话不回收 → 内存耗尽；影响 2.0.0/2.0.1/2.1.x，修复 2.2.0）。pyproject/uv.lock 上游仍钉 `mcp==2.0.0`，**任何 uv sync / hermes update 都会把它退回 2.0.0** | `~/.hermes/hermes-agent/venv/bin/pip install 'mcp==2.2.0'` → 校验 `venv/bin/python -c "import importlib.metadata as md;print(md.version('mcp'))"` 应为 2.2.0 |

- 事故记录（2026-08-24）：hermes update 07:28 uv sync 重建 253 包，清除手动安装的 baostock
  → 08:20 因子·全市场覆盖扩展 / 09:05 因子·ETF缓存刷新 ModuleNotFoundError。
  修复：重装 baostock 0.9.3 + 补跑两任务 exit=0。
- 防复发：升级后运行 `~/.hermes/hermes-agent/venv/bin/python3 -c "import baostock"` 验证，失败即重装。

## 本地补丁（cron/*.py 等源码定制）

### P1. Lark WS 多实例 event loop 竞态修复（thread-local proxy）— 2026-08-20 建立 / 08-25 重放

- **文件**: `plugins/platforms/feishu/adapter.py`（`_run_official_feishu_ws_client` 函数）
- **问题**: lark_oapi ws/client.py 模块级全局 `loop`（Issue #119，官方未修）。双实例各自线程
  `ws_client_module.loop = <loop>` 互相覆盖 → 断连重连时 SDK `loop.create_task()` 拿到另一
  实例 loop → `RuntimeError: Future attached to a different loop` → 接收循环死、SDK 不重连 →
  入站黑洞。8/19、8/20、8/25 三次实证。
- **修复**: 模块级 `loop` 只安装一次 `_ThreadLoopProxy`（thread-local 解析，每线程拿到自己
  loop），用 `_hermes_thread_loop_proxy_installed` 标记防重复安装。
- **重放验证**: `grep -n "_ThreadLoopProxy\|_hermes_thread_loop_proxy_installed" plugins/platforms/feishu/adapter.py`
  有输出即已重放；缺失则从 git 历史恢复：`git show c1bd8df81 -- plugins/platforms/feishu/adapter.py | git apply --3way`
- **回归测试**: `venv/bin/python -m pytest tests/gateway/test_feishu_thread_loop_proxy.py`（4 场景；
  裸赋值版下 3/4 失败已验证防回归有效。测试文件 97e50d9a3，升级前确认仍在）
- **✅ 复核 2026-09-27（判绿 → 不开 PR）**: 上游已用同一机制修掉 → commit `14f20d142e`
  `fix(feishu): isolate lark_oapi WS globals per profile and supervise the client thread (#73779)`
  （2026-09-02，已是 `origin/main` 祖先）。实证：`origin/main` adapter 含 `_ThreadLocalLoopProxy`
  （1035 定义 / 1057 `ws_client_module.loop = _ThreadLocalLoopProxy(...)`），与本地块**逐行同源**，
  唯一差异是 `real_connect` 结果处理（属 P5 握手信号，已由 #124905 覆盖）。**本地补丁暂留 active**
  （本仓仍在旧谱系），升级窗口落 14f20d142e 后退役（`category→deprecated` + `active=False`）。

### P2. lifecycle_guard 安全重启通道豁免 — 2026-08-20 建立 / 08-25 重放

- **文件**: `cron/lifecycle_guard.py`
- **内容**: `is_safe_gateway_restart_invocation()` — 显式调用 `gateway_safe_restart.py` 且带非空
  `--reason` 的受控重启豁免生命周期拦截（审计留痕 + systemd-run 提交）。
- **重放验证**: `grep -n "is_safe_gateway_restart_invocation" cron/lifecycle_guard.py`；
  缺失时 `git show c1bd8df81 -- cron/lifecycle_guard.py | git apply --3way`

### P3. systemd multiplex sticky profile — 已由上游 v0.21.0 吸收（2026-09-01 弃用，留档）

- **原本地补丁**: commit 7d3e01e00（`_is_root_gateway_run()` 豁免，2026-08-25 env 污染根治）
- **问题**: `~/.hermes/active_profile`（`hermes profile use` 写入）使任何 hermes_cli.main 进程
  启动时把 HERMES_HOME 覆盖为 `profiles/<name>`，systemd unit 的
  `Environment=HERMES_HOME=<root>` 被代码逻辑覆盖 → gateway 子进程（cron/terminal/CLI）
  全继承污染 env：08-21 测试 19/22 假失败 / 08-22 `hermes config set` 误写 game-dev
  config.yaml+.env / 08-25 健康检查 cron 读 game-dev 日志 → feishu_instances=0 误判 →
  default 入站黑洞 2h 静默。
- **上游 v2026.8.31 等价实现**: `_apply_profile_override()` 内 `_under_gateway_supervisor()`
  （识别 HERMES_SUPERVISED_CHILD / HERMES_S6_SUPERVISED_CHILD / INVOCATION_ID /
  HERMES_GATEWAY_EXTERNAL_SUPERVISOR）+ step 1.5（HERMES_HOME 指向 profiles/ 时不跟随
  sticky）—— 比本地补丁更通用，覆盖 systemd 部署（INVOCATION_ID 兜底）
- **弃用原因**: 本地 `_is_root_gateway_run` 检查 argv 模式，上游用环境标记判断，语义等价且更全；
  按 08-14 sidebar 先例主动丢弃定制
- **验证**: `grep -n "_under_gateway_supervisor" hermes_cli/main.py`；升级后 gateway 进程环境含
  INVOCATION_ID（systemd 服务子进程必有）

### P4. cron 增量预算创建时拦截（P3-1, 2026-08-25 用户批准 945061；2026-09-01 v0.21.0 升级重放）

- **文件**: `cron/jobs.py`（新增 `_CRON_GROWTH_LIMIT_DEFAULT` + `_check_job_growth_budget()`，
  `create_job()` 内 label_source 计算后调用）
- **问题**: 近30天新增任务上限 20 只有事后检查（08:30 合规报告），拦不住创建行为本身
  （2026-08 连报多日仍新增 94 个）。改为创建时即拒绝。
- **语义**: 现存 jobs.json 中 created_at 在近30天窗口内的任务数 >= 上限 → raise ValueError
  拒绝创建（含一次性任务）；删除配平立即降计数。紧急通道 env `HERMES_CRON_BUDGET_BYPASS=1`；
  阈值覆盖 env `HERMES_CRON_BUDGET_LIMIT`（测试/调参）。jobs.json 读失败保守放行。
- **阈值 20 → 30（2026-09-17 用户批准，待办 461530）**：配平后（删 2 死任务 + 合并 1 组重复覆盖）
  近 30 天仍有 24 个，逐条核对**全为系统防线/采集/投研任务、零浪费**（脚本无重复、last_status
  全 ok、streak 全 0）→ 20 已成常态建设硬顶，只会逼出 BYPASS 或砍在役防线；30 仍距失控域（+94/月）
  3 倍。**三处必须同口径**：`cron/jobs.py::_CRON_GROWTH_LIMIT_DEFAULT`、
  `~/.hermes/scripts/cron_governance_weekly.py::LIMIT`、`~/.hermes/scripts/architecture_compliance_check.py::check_cron_growth().limit`。
- **口径分层（2026-09-25 用户按推荐执行，待办 461346）**：计数从「近 30 天新增（含一次性）」
  改为「近 30 天**在役增量**」= 常驻任务一律计入 + 一次性任务只在仍可调度时计入
  （跑完即 `_complete_job_record` 置 `enabled=False` + `state=completed` → 不再占额度）。
  触发实况：09-25 实测在册 30/30 已满、`create_job` 直接拒绝，其中 12 条为跑完的一次性
  （夜批/复核 churn ≈4 个/日）；落点计数 30 → 18（常驻 13 + 在办一次性 5）。
  **实现面 6 处（缺任一处即口径分裂/升级静默回退）**：① 内核 `cron/jobs.py::is_active_increment`
  + `_check_job_growth_budget` ② 脚本侧唯一实现 `~/.hermes/scripts/cron_budget_scope.py`
  （两个消费点改为 import 它，不再各写一份） ③ `cron_governance_weekly.py`
  ④ `architecture_compliance_check.check_cron_growth()` ⑤ `verify_patches.sh` 的 P4
  口径断言（4 条） ⑥ 本文件。
  **对冲项刻意不设门**：子闸门「未完成一次性 ≤8」的 8 无历史分布依据 → 只作观察值
  （`scope_observation()`：在办 / 窗口内新建 / 已跑完，随周治理输出打印），跑满一个窗口
  再谈是否设门（无依据的阈值不得告警、不得拒建）。
- **回归测试**: `scripts/tests/test_cron_budget_scope.py`（17 例：真值表 11 场景 + 真库逐条
  同判定同计数 + 跑完的一次性不计 + **停用常驻不占额** + 窗口边界 + 观察值落盘 2 例）；
  隔离 A/B = `HERMES_HOME=<tmp>` 合成副本上**旧谓词拒 / 新谓词放行**（差异恰等于停用常驻条数）
  + 生产 jobs.json hash 前后一致。
- **重放验证**: `grep -n "_check_job_growth_budget" cron/jobs.py` 有输出即已重放；
  缺失则 `git show 4bec14cd5 -- cron/jobs.py | git apply --3way`；阈值部分单独重放用
  `git show 44920c5a87 -- cron/jobs.py | git apply --3way`（或直接把常量改成 30）。
  重放后**须核对阈值 = 30 且口径 = 在役增量**（`bash ~/.hermes/scripts/verify_patches.sh` 的
  「P4阈值=30/两处同步」+「P4口径」共 11 项会同时拦住改回 20、退回旧口径、丢掉在役对齐）
- **在役对齐（2026-10-16 用户按建议执行，wj 615105）**：`is_active_increment` 从「常驻一律计入 +
  一次性按 `is_job_runnable()`」改为**两者同一判定** `is_job_runnable()`（enabled 且未暂停）。
  动因：谓词不对称 ⇒ **停用常驻不腾额度**、只剩「删除」一条释放路径（与「防线该停用不该删」相冲）；
  同日实测 30/30 顶格中载体侧占 14（47%），缺一条零成本释放路径。
  影响面：阈值 30、窗口 30 天、拦截点（仅 create 路径）均不变；当日 16 条在役常驻全 enabled ⇒
  **计数 30 不变、零行为漂移**（A/B 实测：生产数据新旧计数相等；合成副本上旧谓词拒 / 新谓词放行）。
  实现面仍是 6 处（内核 + 脚本侧 SSOT + 两个消费点注释 + 本文件 + `verify_patches.sh` P4 新增 2 项断言）；
  `scope_observation()` 新增 `standing_active` 键（在役面），`standing` 语义明确为**结构面**
  （窗口内常驻总数，含停用）—— 加键不改形状，历史行无该键 ⇒ 消费方一律 `.get`。
  **生效点**：内核在网关/调度器进程内 import ⇒ 需变更探针合规重启后才生效（判据：进程启动时刻 ≥
  `cron/jobs.py` mtime）；脚本侧（`cron_budget_scope.py`）下次调用即生效。
  回滚：`cp -a ~/.hermes/data/script_baselines_pre_change/jobs.py.pre-o3align-* ~/.hermes/hermes-agent/cron/jobs.py`
  （脚本侧 4 文件同名 pre-image 在 `~/.hermes/data/script_baselines_pre_change/`）。

### P5. Feishu fake-alive 守卫：等待 SDK 真实握手 + start() 异常可见 — 2026-09-03 建立

- **文件**: `plugins/platforms/feishu/adapter.py`（`_run_official_feishu_ws_client` / `_connect_with_overrides` / `_connect_websocket` / `connect()` / `__init__`）
- **问题**: default 实例入站黑洞（出站正常），13:31 lark-health 自动重启 + 13:40 manual 重启均不愈。
  根因链：`_connect_websocket` 只 `run_in_executor` 提交 SDK 线程即乐观返回 → `connect()` 打
  `Connected in websocket mode`（假活）；SDK `ws_client.start()` 失败被 `except Exception: pass`
  静默吞 → 零日志、SDK 层从未建立 wss 连接（agent.log `Lark: connected to wss` 行数 < 实例数）、
  重启无法治愈。对比：同进程 game-dev 实例正常 → 排除进程/网络整体故障。
- **修复**: ① `_connect_with_overrides` 在真实 `websockets.connect` 成功后 set
  `adapter._ws_connected_event`；② `_connect_websocket` 提交线程后 `wait_for(event, 25s)`，
  超时 raise → `_connect_with_retry` 指数退避重试 → `connect()` 报 startup failed 而非乐观
  Connected；③ start() 的 except 改为 `logger.error(exc_info=True)`（app_id 尾号 + thread id），
  不再吞异常。常量 `_FEISHU_WS_CONNECT_CONFIRM_TIMEOUT = 25.0`。
- **重放验证**: `grep -n "_ws_connected_event\|fake-alive guard" plugins/platforms/feishu/adapter.py`
  有输出即已重放；缺失时 `git show b32d50a3e6 -- plugins/platforms/feishu/adapter.py | git apply --3way`
- **升级后判定双实例真实恢复**: agent.log `Lark: connected to wss://` 行数 ≥ feishu 实例数（双实例=2），
  不能只看 adapter 层 `Connected in websocket mode`（乐观打点）。
- **回归测试**: 无独立单测（依赖真实 SDK 握手）；`py_compile` 通过。重启后数 SDK 层 connected 行验证。

- **上游化 (2026-09-27) → PR #124905**（分支 `fix/feishu-ws-handshake-proof`，基线 origin/main `ddb56d0aa1`，
  worktree `/tmp/pr-p10-deepseek`，未动 default 生产树）。上游侧**已有判活信号**（SDK receive-loop 入口
  `_on_link_up` → `adapter._ws_link_up`，与 #73779 的 `_supervise_websocket_thread` 同源），故上游化形态与
  本地补丁有 **三处有意差异**，上游合入后本地按上游收敛：
  ① 证明来源：本地 = 自建 `_ws_connected_event`，在 `_connect_with_overrides`（上游无此函数）里由自有
     `websockets.connect` 成功后 set；上游 = 直接等 `_ws_link_up`（`self._ws_link_up_event`），语义相同
     （提交线程 ≠ 已连接）、机制不同。常量名 `_FEISHU_WS_CONNECT_CONFIRM_TIMEOUT` → `_FEISHU_WS_HANDSHAKE_CONFIRM_TIMEOUT`
     （值同为 25.0，落在网关 30s 单平台连接预算 `_PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT` 内）。
  ② 上游化版**新增身份校验**（本地版无）：只有当前持有 client 的 link-up 能放行本次等待
     （`self._ws_client is ws_client`），退避重试时旧线程的迟到信号不会冒充；`disconnect()` 与重试路径清 event。
  ③ `start()` 异常细节（本地 ③）：上游 `_run_official_feishu_ws_client` 仍是 `try: ws_client.start()
     except Exception: pass`（**异常本体仍被吞**），supervisor 只补上「线程意外退出 + ws_link_lost/retrying 状态」，
     故本地 `logger.error(exc_info=True)` **本轮未纳入 #124905**，留在本地；若要上游化，作为独立小 PR 改该 except。
  - 测试与证据：`tests/gateway/test_feishu_ws_handshake_guard.py` 3 例不变量（base 3 failed → patched 3 passed）；
    2 个既有用例按新契约补报 link-up（断言未改）；回归 `tests/gateway/test_feishu*.py` + `test_adapter*.py`
    24 文件与回退树失败集一致（311 passed / 2 条既有环境失败）。
  - **同文件排序（2026-09-27 拍板选 (a)）**：与 P7/M2/P1 同文件，**不打包**——P7 待 #124905 合入后以新 main
    为基线另开 PR（`PR_INTENT["P7"]="blocked"`）；#124905 合入前本地 P5 保持 active。

### P6. shutdown drain: watchdog leash 纳入 cron_drain_timeout（防重启掐断 in-flight cron）— 2026-09-03 建立

> **移植注记 2026-09-10（v0.21.1）**: 上游已将 shutdown 逻辑从 `gateway/run.py` 拆到 `gateway/run_shutdown.py`（`GatewayShutdownMixin`）。本补丁 3 处 leash 已同步移植至该文件，语义不变（`max(restart_drain_timeout, cron_drain_timeout)` 作 watchdog leash）。

- **文件**: `gateway/run_shutdown.py`（3 处 watchdog leash 计算：诊断快照 `watchdog_delay_s` / `arm_shutdown_watchdog` delay / `resolve_cron_drain_budget` ceiling）
- **问题**: 2026-09-03 19:21 合规重启 60s drain 超时后强杀 in-flight **KB·半导体充实(19:15二四六)**（79fafef419b5，已跑 6min 的 LLM 任务）→ last_status=error，下次调度要等周六。根因：① `config.yaml agent.cron_drain_timeout` 默认 30s 且本机未显式配置（被 `restart_drain_timeout=60` 顶掉）；② 即使调大配置，shutdown watchdog leash = `resolve_shutdown_watchdog_delay(restart_drain_timeout)`（60+60=120s）作 ceiling 把 cron budget 压回 ~108s，且 watchdog 120s 硬退出（os._exit）比优雅 interrupt 更糟。
- **修复**: 三处 leash 由 `restart_drain_timeout` 改为 `max(restart_drain_timeout, cron_drain_timeout)`：
  ① `_shutdown_watchdog_snapshot` 的 `watchdog_delay_s`（诊断日志真实值）
  ② `arm_shutdown_watchdog(...)` 的 delay（shutdown watchdog leash = max+60 grace = 960s）
  ③ `resolve_cron_drain_budget(watchdog_delay=...)` 的 ceiling（900s budget 真实生效）
  配套（非源码）：`config.yaml agent.cron_drain_timeout: 900`（hermes config set 写入，444 未掉）；
  `~/.config/systemd/user/hermes-gateway.service TimeoutStopSec 90→940`（≥900 drain + 10 cleanup + 30 headroom，防止 systemd SIGKILL 截断 in-budget drain）
- **行为**: 重启/停止时 chat 回合仍 60s 掐（restart_drain_timeout 不变），in-flight cron 单独等至多 900s 自然完成；无 cron 在跑时 drain 即刻返回（非傻等）。
- **重放验证**: `grep -n "max(timeout, _cron_drain_cfg)\|max(\s*self._restart_drain_timeout" gateway/run_shutdown.py` 命中 + config 含 `cron_drain_timeout: 900` + unit `TimeoutStopSec=940`（v0.21.1 移植后路径）
- **数值验证**: `venv/bin/python -c` 调 `resolve_cron_drain_budget(60,900,watchdog_delay=resolve_shutdown_watchdog_delay(max(60,900)))` = 900.0；`resolve_systemd_timeout_stop_sec(60,900)` = 940
- **升级注意**: 上游若已修（#82161/#94759 注释显示上游 awareness），以 `git log gateway/run.py` 比对后优先取上游实现，本地补丁可弃
- **✅ 复核 2026-09-27（判「上游契约相反」→ 不开 PR，改判 local-policy）**: 上游 leash 定义为
  `resolve_shutdown_watchdog_delay(drain)` = `restart_drain + grace`（**无 max**），且 cron floor 被**显式**
  裁在该 leash 之下：`_cron_leash = effective_stop_watchdog_delay(self, resolve_shutdown_watchdog_delay(timeout))`
  → `resolve_cron_drain_budget(..., watchdog_delay=_cron_leash, ...)`（`gateway/run_shutdown.py` 1849/1850），
  另一处 leash 用法在 153 行（`effective_stop_drain_timeout(runner)` 入参）。即「`cron_drain_timeout`
  调大也拿不到对应等待时长」是上游**有意**设计，不是漏修；本仓需要 900s 真正生效（6–20min cron 任务不被重启掐断），
  立场与之相反 → `category=local-policy`、`pr_candidate=False`、移出 PR_INTENT，**本地补丁保持 active**。
  备选方案（日后可改选）：向上游提「leash 覆盖 `max(restart_drain, cron_drain)`」的策略变更，
  代价=改动 watchdog 硬退出时间语义 + 需同步 unit `TimeoutStopSec`（本仓 940s），且同点位有在途第三方 PR。
- **决策注记 2026-09-05（待办 693615 裁决）**: 900s **保留不降**。①调优对象澄清：`restart_drain_timeout=60` 是 chat 回合 budget（不动），in-flight cron 等待时长=`cron_drain_timeout`（P6 三处 leash max 化）；②降 120-180s 会回归 P6 修复前状态——KB·半导体充实（6min+ LLM 任务）等中等 cron 重启即被强掐，丢整轮任务等下次调度；X8f 月度（20min）无论 900/180 均会被掐，调短对超长任务无益；③今早 08-05 B期迁移重启 15min（900s drain 耗尽）根因=变更撞 in-flight 月度 cron 未避让，X8f 差 41s 即自然完成（900 几乎放行到底）；④纪律固化：gateway 重启/变更前查 in-flight cron（jobs.json running），撞长 cron 等完成或错峰——900s 仅作兜底不设主动等待。变更执行须先查再重启（同 764744 演练步骤）。

### ✅ 08-24 reset 丢失的本地提交 — 已于 2026-09-01 v0.21.0 升级全部重放

- `c1bd8df81` — Lark WS thread-local proxy 修复 → 已重放（0001）
- `e7b969131` — fix(cron): cron agents 禁用 memory（恢复 v0.20.4 行为）→ 已重放（0002，scheduler.py skip_memory=True 验证通过）
- `6194c3dad` — feat(feishu): FEISHU_BOT_MENTION_MAP 出站 bot->bot 真实提及 → 已重放（0002，adapter.py 4 处验证通过）
- 历史记录（备查）：08-24 reset 后曾登记为「仍丢失待恢复」，本次升级随 format-patch 重放全部恢复

### P7. 网关退出卡 300s：Feishu WS 线程移出 default executor + Runner join 压至 15s — 2026-09-04 建立

- **文件**: `plugins/platforms/feishu/adapter.py` + `gateway/run.py` + `hermes_cli/gateway.py` + `cli.py`
  （v0.21.1 移植后: helper `run_with_bounded_executor_shutdown` 仍定义于 `gateway/run.py`，三处入口调用形式不变；adapter 侧由一期移植承接）
- **问题**: 2026-09-04 11:23 合规重启（stale-code 触发）实际中断 5:07（设计 ~8s）。根因链：
  ① `_connect_websocket` 把长生命周期 WS SDK 线程 `run_in_executor(None, _run_official_feishu_ws_client)` 丢在
  **asyncio 共享 default executor**（与 #10849 已修的自持 `_sdk_executor` 不一致）；
  ② game-dev 实例 disconnect 时 `run_coroutine_threadsafe(_disconnect())` 5s 未完成（CLOSE 帧未发出）→ run.py
  外层 5s 预算超时 cancel+detach（`_bounded_adapter_teardown`）→ 没等到内部 10s ws 退出窗口 → WS 线程卡半死连接；
  ③ `asyncio.run` 的 `Runner.close()` join default executor 硬 300s（`THREAD_JOIN_TIMEOUT`），**发生在
  os._exit 兜底（run.py `_exit_after_graceful_shutdown` / hermes_cli.gateway `_hard_exit_after_gateway_teardown`，
  #53107）之前** → 兜底没机会执行 → 300s 后 systemd 才拉起。
  附带影响：tier1 报 gateway/weixin 异常+恢复（一级群噪音），真实通道中断 5min。
  ⚠️ **入口陷阱（首验失败教训）**: systemd ExecStart=`python -m hermes_cli.main gateway run` → 实际入口是
  `hermes_cli/gateway.py run_gateway()` 的 asyncio.run，**不是 gateway/run.py main()**（后者仅直跑
  `python gateway/run.py` 用）。首轮只改 run.py main() → 重启仍 300s（实证 11:41 复现卡死，py-spy 因
  ptrace_scope=1 无法 attach，靠 cmdline 溯源定位）。三个入口必须同改。
- **修复**（adapter.py）: 新增 `_get_ws_executor`/`_shutdown_ws_executor`（复制 `_sdk_executor` 锁+closing+recreate 模式，
  `thread_name_prefix="hermes-feishu-ws"`, max_workers=2）；`_connect_websocket` 两处 `run_in_executor(None,...)`
  （WS client + conn_evt.wait）改走自持池；`disconnect()` 在 `_shutdown_sdk_executor()` 后一并
  `_shutdown_ws_executor()`（CancelledError 路径也覆盖——detach 后清理仍执行到该行）。
- **修复**（run.py / hermes_cli/gateway.py / cli.py）: 新增公共 helper `gateway.run.run_with_bounded_executor_shutdown(coro, timeout=15)`
  （手动 `asyncio.Runner`：`runner.run()` 后先 `loop.shutdown_default_executor(timeout=15)` 再 `runner.close()`——
  shutdown 幂等，close 内二次调用直接 return）。替换全部三个 asyncio.run(start_gateway) 入口：
  `gateway/run.py main()`、`hermes_cli/gateway.py run_gateway()`（systemd 实际入口）、`cli.py` gateway 分支。
  任何 default executor 泄漏的最坏等待从 300s 压到 15s。
- **验证**: 直接脚本 4 项全过（WS 任务线程名前缀 hermes-feishu-ws / default=asyncio_0 证明移出 / closing 阻止重建 /
  re-arm 后重建 / sdk executor 回归）；三文件 py_compile + AST OK；`test_feishu_lazy_import.py` 过（`test_feishu_sdk_executor.py`
  async 用例需 pytest-asyncio，环境未装——预存缺口，非本次引入）。首验失败记录：只改 run.py main() 未覆盖 systemd 实际入口
  （hermes_cli.gateway），11:41 重启复现 300s 卡死；三入口全改后待真实重启验证（判据见 gateway-lifecycle-ops）。
- **重放验证**: `grep -n "_get_ws_executor\|_ws_executor_closing" plugins/platforms/feishu/adapter.py` +
  `grep -rn "run_with_bounded_executor_shutdown" gateway/run.py hermes_cli/gateway.py cli.py`（3 处入口各 ≥1）
- **升级注意**: 上游若吸收（相关 #10849/#53107 生态），以 git log 比对后优先上游实现；本地补丁可弃。
- **上游化排序 (2026-09-27 拍板，选 (a) 不打包)**: P5 与 P7/M2/P1 同文件 `plugins/platforms/feishu/adapter.py`，
  但**不合并成一个 PR**——P5 已单独开 PR #124905（fake-alive 守卫），P7 待其合入后**以新 main 为基线**另开 PR。
  理由：两处修的病不同（假活 vs 退出卡死），同 PR 会被要求拆分，且同文件文本重叠需 rebase。
  patch_manifest `PR_INTENT["P7"]="blocked"` 登记该前置（非「计划未开」）。

### P8. 本地 provider 插件 web-bocha（博查搜索）纳入版本管理 — 2026-09-06 补提交

- **文件**: `plugins/web/bocha/`（plugin.yaml + provider.py，全新目录,非修改）
- **背景**: 博查(Bocha) 中文多源搜索 provider, 2026-08 本地开发适配（bocha_search.py 演进为插件形态）。
  开发后未提交、config `plugins.enabled` 未启用（当前 web 后端走 searxng/exa, 121975 修复后）。
  8-31 v0.21.0 升级 reset 后幸存因 reset 不删 untracked——属游离文件, 升级走独立 clone 同步方式时可能丢失。
- **登记**: 纳入 git 版本管理 + 本台账。未启用, 作为备用后端（.env 已备 BOCHA_API_KEY）。
- **启用路径（若日后切后端）**: `hermes config set plugins.enabled +web/bocha` + `hermes config set web.search_backend bocha` → web_backend_smoke.py 实测。
- **验证**: provider.py py_compile 通过; plugin.yaml provides_web_providers: [bocha] 与 web provider 注册约定一致。

### P9. executions.db 保留窗口 env 化恢复（HERMES_CRON_EXECUTIONS_MAX）— 2026-09-09 审计 D3-P1-1

- **文件**: `cron/executions.py`（L24 `MAX_TERMINAL_EXECUTIONS = int(os.getenv("HERMES_CRON_EXECUTIONS_MAX", "1000"))`）
- **问题**: 运行时 env 覆盖曾于 2026-08-10 落地，被后续升级覆盖丢失 → 保留窗口硬编码回归 1000 行(≈6.5h)。
  后果: interrupted_detector 声称的「近24h窗口」实际仅 ~6.5h；>6.5h 长停机(8/6 SSL、8/7 OOM 类)时中断记录在
  检测器(每30分)来得及看之前即被剪，自动补跑 v2 目标场景失效。.env 与 systemd drop-in 配的 10000 成死配置。
- **修复**: import 时读 env（保留测试 monkeypatch 模块属性可注入性——勿改回函数内 getenv，会打破
  test_retention_bounds_terminal_history_but_preserves_inflight 的 monkeypatch 约定）。
- **重放验证**: `grep -n "HERMES_CRON_EXECUTIONS_MAX" cron/executions.py` 有输出即已重放；
  `venv/bin/python -c "import os;os.environ['HERMES_CRON_EXECUTIONS_MAX']='42';import cron.executions as e;print(e.MAX_TERMINAL_EXECUTIONS)"` 输出 42 即 env 生效。
- **2026-09-27 扩写（待办 677072 分级保留 + prune 节流）**:
  旧语义是**全局配额**「只留最新 MAX 条终端行」→ 实测 10000 行 = 2.86 天，单个分钟级 job
  (`2e3be3f33c61` 系统·一级状态机，每 1 分) 独占 40.6%，把周/月级任务的派发记录挤出
  （`cron_missed_detector` 需靠产出文件兜底，待办 250244 的 30/191 缺口即此）。
  新语义（`_prune_unlocked`）: ① `failed/unknown` 按时间保留
  `HERMES_CRON_FAILURE_RETENTION_DAYS`（默认 30 天，不受配额影响）
  ② `completed` 在 `HERMES_CRON_SUCCESS_FLOOR_DAYS`（默认 7 天）内全留，超出后每 job 至少留
  `HERMES_CRON_PER_JOB_KEEP`（默认 200）条，再受全局硬顶 `HERMES_CRON_EXECUTIONS_MAX` 约束
  ③ claimed/running 永不删 ④ prune 节流：未超硬顶时按
  `HERMES_CRON_PRUNE_MIN_INTERVAL_SECONDS`(60s) / `HERMES_CRON_PRUNE_EVERY_N_FINISHES`(20) 跳过，
  **超硬顶立即执行**（保住上游 `test_retention_bounds_terminal_history_but_preserves_inflight` 语义）。
  同时 cap 10000 → 30000（`.env` + `cron-timeout.conf` 双处；须重启 gateway 生效）。
  **索引决策（实测后弃用）**: 原计划建 `(finished_at DESC …)` 部分索引；EXPLAIN 实测硬顶计数与
  超期失败行清理已由既有 `idx_executions_status_claimed`(COVERING) 服务，硬顶删除的
  `status='completed'` 谓词又不匹配该部分索引（partial index 谓词必须被查询条件蕴含）→
  无采用点、只增写代价，**决定不建**。
- **回归**: `venv/bin/python -m pytest tests/cron/test_execution_ledger.py -q`（上游 25 例）
  + `python3 scripts/tests/test_executions_prune_policy.py`（本策略 14 例）必须全绿。
- **升级注意**: 上游若吸收，以 git log 比对后优先上游实现；本地补丁可弃。
- **2026-10-19 扩写（待办 728654 · 修「超顶判定 vs 修剪对象」口径不一致）**:
  - **病**: `_prune_unlocked` 的超顶判定数**全部终态行**（completed+failed+unknown），修剪却只删
    completed（失败行仅按 30 天保留期删）⇒ completed 逼近硬顶、保留期内失败行把它推过线时，
    节流判「超顶 → 立即执行」，于是**每次终态写入都跑一遍全表排序删，却始终删不动**，
    行数持续超顶（设计意图是未超顶时按 60s / 累计 20 次节流以降写放大）。
    v0.22.2 新增 `tests/cron/test_restart_safe_worker_death_evidence.py::test_adopted_worker_failure_is_visible`
    （5 死法 × manual 两臂 = 10 例）断言 `len(list_executions()) <= MAX_TERMINAL_EXECUTIONS`，
    在本地树上 3<=2 全红 —— 上游契约要求「硬顶是绝对上界」。
  - **修**: 硬顶语义改为「**度量与修剪同批行**」：硬顶仍按全部终态行度量（跟随上游），
    但配额分配改为**失败/未知行优先占位**，completed 只能用 `cap - 失败行数` 的剩余额度
    （先回收 per-job 溢出、再全局最旧兜底）；仅当失败行自身超顶（病态）才按最旧回收失败行。
    即：正常态失败行仍享 30 天保留；硬顶压力下**先让 completed 让位**，失败证据最后才动。
    与上游 PR #124830 的方向一致（该 PR 亦把硬顶扩到失败行：同分序 oldest → unknown → failed）。
  - **不变式保持**: ① claimed/running 永不删 ② 超硬顶时即使未 force 也立即 prune（⑦ 段用例）
    ③ 失败行 30 天保留（无硬顶压力时）④ per-job 保底（硬顶绑定下让位，同 846814 口径）。
  - **验证（实测）**: `tests/cron/test_restart_safe_worker_death_evidence.py` 11 passed（修复前 3<=2 红）;
    `tests/cron/test_execution_ledger.py` 24 passed; `scripts/tests/test_executions_prune_policy.py`
    17 检查全 PASS（新增 3 组：失败行推过线时未 force 即收敛 / 最新失败行不被误删 /
    失败行自身超顶才回收）。
  - **口径变更留痕**: 本地契约测试的 ③/⑤ 判据从「硬顶只作用于 completed（失败行不受影响）」改为
    「硬顶是绝对上界，失败行优先占配额」（同一轮改测试，避免测试与实现反向漂移）。
- **上游化 (2026-09-27) → PR #124830**（分支 `fix/cron-execution-ledger-retention`，基线 origin/main
  `79dbb1450e`，worktree `/tmp/pr-p9-executions`，未动 default 生产树）。上游化形态与本地补丁有
  **三处有意差异**，上游合入后本地按上游收敛：
  ① env → **config 键** `cron.executions_{success_floor_days,failure_retention_days,per_job_keep,
     max_terminal_rows}`（prune 时读取、不落 `DEFAULT_CONFIG`，与既有 `cron.completed_retention_days`
     同型；上游拒以 env 承载非密钥配置，且 `DEFAULT_CONFIG` 声明会遮蔽模块常量并打破 monkeypatch 缝）。
  ② **硬顶语义修正 → 本地缺陷**: 本地版「每 job 保底 200 条」在硬顶绑定（=常态：10000/30000 行被单个
     分钟级 job 独占）时是 **no-op**；上游版改为「先驱逐**行数最多**的 job 的**最旧**行」（同分
     oldest → unknown → failed），保底才真正生效。**本地版待按此修订**（上游未合入前先记录）。
  ③ 三处保留下限由「模块常量」改为「默认值」，测试仍走 monkeypatch 缝（`test_execution_ledger.py`
     25 例在两种 shape 下均通过）。
- **上游化红绿证据**: 新 `tests/cron/test_execution_ledger_retention.py`（3 例不变式）在 main 上
  3 failed（`the quiet job lost its history…` / `cap pressure discarded failure evidence` /
  `AttributeError: no attribute 'SUCCESS_FLOOR_DAYS'`），补丁后两文件 `26 passed`；
  全量对照 `tests/cron/`(73 failed，与 pristine main 逐文件同) + `tests/tools/`(23 files / 99 failed，
  与 pristine main **零差异**)。**未合入前本地补丁保持 active**。
- **上游化进度 (2026-10-11 rebase 完成)**: PR #124830 旧头 `de8ae1108d` 与 `origin/main` 冲突已解并 force-push
  （lease 比对）→ 新头 `eae84f044670f4688e822ad2fa6d8428dee36881`，API 回读 `mergeable: true`。冲突属
  「上游同改函数体」：上游新增 `_stale_age_seconds` / `touch_execution_progress`，本 PR 整体重写 `_prune_unlocked`
  → 用 `git checkout origin/main -- cron/executions.py` + `git apply -3` 保留双方（残留仅函数头一处手工收口）。
  证据（新 base）：`tests/cron/` 全目录 155 files **1285 passed / 202 failed / 3 skipped**，202 与 pristine `main`
  （154 files / 1280 passed / 202 failed）**同一失败集合**（本机环境型；差 1 文件 5 用例 = 本 PR 新增模块）；
  红态（仅回退 `cron/executions.py`、保留测试）2 files **24 passed / 5 failed**，5 条即新模块的保留策略不变式。
  P9 语义待按上游收敛（本地版 per-job 保底在硬顶绑定下为 no-op，待办 846814）——不影响本 PR 已可合。

### P10. DeepSeek 新模型名 `deepseek-flash`（V4.1-Flash 更名）规范化适配 — 2026-09-10

- **文件**: `hermes_cli/model_normalize.py`（`_DEEPSEEK_CANONICAL_MODELS` +1 项 + docstring）+
  `tests/hermes_cli/test_model_normalize.py`（`TestDeepseekFlashRename20260910` 3 用例）
- **问题**: 官方 2026-09-10 更名 flash 为 `deepseek-flash`（V4.1-Flash；`GET /v1/models` 只返回
  `deepseek-flash` 与 `deepseek-v4-pro`）。该 id 不含 `v<digit>` → 不匹配 `_DEEPSEEK_V_SERIES_RE`
  → 被兜底分支反向改写成旧 id `deepseek-v4-flash` 再发出。后果：CLI/`state.db sessions.model`
  长期记旧名，运维按名判断会误判；且主链路被动依赖官方继续接受「已更名」的旧 id，一旦下线即全量
  400（与 2026-07-24 `deepseek-chat`/`deepseek-reasoner` 退役同型）。
- **修复**: 把 `deepseek-flash` 加入 `_DEEPSEEK_CANONICAL_MODELS` 原样透传（官方 `/v1/models` 唯一
  flash 项，实测 200 且响应 `model=deepseek-flash`）。**旧名分支不动**——`deepseek-v4-flash` 继续原样
  发出，兼容面留到官方真下线旧 id 时再收（届时只改同一集合）。`default_aux_model` 故意不动（两 id
  同模型同价，改动只增 diff 面）。
- **重放验证**: `grep -nE '"deepseek-flash"' hermes_cli/model_normalize.py` 有输出即已重放；
  `venv/bin/python -c "import sys;sys.path.insert(0,'.');from hermes_cli.model_normalize import normalize_model_for_provider as N;print(N('deepseek-flash','deepseek'))"`
  输出 `deepseek-flash` 即生效（修复前输出 `deepseek-v4-flash`）。
- **回归测试**: `venv/bin/python -m pytest tests/hermes_cli/test_model_normalize.py -q`（26 passed；
  基线 23）。端到端：新开 CLI 会话后 `state.db sessions.model` 应为 `deepseek-flash`（实测
  20260910_163350_bd7d59 = deepseek-flash，前序会话为 deepseek-v4-flash）。
- **生效面**: 进程启动时加载 → **CLI 新会话立即生效；飞书侧需 gateway 重启**（不为本项单独重启
  tier1，等下次自然/维护重启生效）。
- **升级注意**: ~~上游 main（2026-09-10 fetch，8068c09432）同样未跟进更名 → 属上游通用缺陷，本地补丁
  为临时兜底；已纳入待办 945249 的上游 PR 候选清单，PR 合并后本地补丁可弃。~~
  **【2026-09-27 更正】该判断已过期**：上游 2026-09-10 当日即以 `6964eebd35` 修掉（pass-through 修法，比本补丁更彻底），
  故本项**不开 PR**、转「跟随上游」，详见下条「上游化判定」。
- **上游化判定 (2026-09-27)：不开 PR，转「跟随上游」**。worktree `/tmp/pr-p10-modelnorm`（detached `origin/main`
  `ddb56d0aa1`）复核结论：**本补丁要治的症状在上游已不存在**——
  ① 上游 `6964eebd35`（2026-09-10，`fix(deepseek): pass custom model ids through instead of rewriting them`，
     已核验为 `origin/main` 祖先、非本地 HEAD 祖先）把兜底重写整段删掉：`_normalize_for_deepseek` 现只做
     `_DEEPSEEK_RETIRED_ALIASES.get(bare, bare)`（`deepseek-chat`/`deepseek-reasoner` → `deepseek-flash`），
     其余**原样透传**；`_DEEPSEEK_CANONICAL_MODELS` / `_DEEPSEEK_V_SERIES_RE` / 兜底 `deepseek-v4-flash`
     在上游已不存在。故上游处理不但覆盖本补丁，且比本补丁更宽（不再改写任何未知 id）。
  ② 上游测试已断言同一语义：`TestDeepseekVSeriesPassThrough::test_deepseek_provider_preserves_versionless_flash_id`
     （docstring 直指「fold 到 `deepseek-v4-flash` 导致用户选的 id 上不了线」），另有 `deepseek-reasoner → deepseek-flash`
     与参数化透传组（含上游 issue `#107206`）。
  ③ 本机实证：在**未打补丁**的上游树上跑该测试文件 → 31 passed，仅 1 条与本项无关的失败
     （`TestColonProviderPrefixIsStrippedLikeSlash::test_agent_init_strips_prefix_before_the_wire`，纯净树自带）；
     再把本补丁的 3 条断言（provider 路径 `deepseek-flash` 透传 / `deepseek-v4-flash` 仍透传 / `reasoner → deepseek-flash`）
     临时附在上游测试文件尾运行 → **3/3 全绿**（探针用完已还原，worktree 无残留）。
  - **处置**: patch_manifest 移除 `PR_INTENT["P10"]`、`pr_candidate=False`（不再是候选）、`upstream_ref` 改为
    「上游已修 6964eebd35」。**本地补丁暂保持 active**——本仓仍基于 `6964eebd35` 之前的谱系（仍是白名单+兜底重写），
    升级前本地仍需它；待**下次升级窗口**把该提交落进本仓后退役（`category→deprecated` + `active=False`，走 P3 同型处置）。
  - **不强行提 PR 的理由**: 上游已用更彻底的修法解决同一问题；反向提本补丁等于要求上游退回「白名单 + 兜底改写」，
    与 `6964eebd35` 的设计意图相反（符合 skill 步骤 1「前提不成立就停」与战役停止条件「上游已改 → 跟随上游不强行推」）。
- **回归基线异常（预存，非本补丁引入）**: `tests/hermes_cli/test_doctor.py::test_doctor_reports_vercel_backend_diagnostics`
  在 HEAD 版本上同样失败（已用还原法实证），与模型名无关。

### P11. searxng provider 超时放宽 15→30s（恢复丢失的本地定制）— 2026-09-11

- **文件**: `plugins/web/searxng/provider.py`（L30 `http_get_json(..., timeout=15→30)`）
- **背景**: 本站 searxng 不是标准实例，而是 `~/.hermes/scripts/searxng_proxy.py`（五通道聚合：
  通道0 Bocha[中文] → TinyFish[禁用] → 搜狗网页/微信 → Bing 站内/宽泛），中文查询内嵌搜狗反爬
  CAPTCHA 降级，**单查询最坏 ~22s**。上游默认 15s 会截断这类慢查询。
- **丢失史**: 定制首建于 2026-08-23（commit `4653e87fd6`），但早于 v0.21.0 升级的 merge-base →
  `git format-patch <merge-base>..HEAD` 不导出 → 09-01 reset 后**静默消失**，档案未标；v0.21.1
  （09-11）移植清单亦未含。判定依据：`git show v2026.8.31:plugins/web/searxng/provider.py | grep timeout=` = 15。
- **实证影响**: 2026-09-11 09:17 一次中文 `web_search` **17.70s 超时失败**（当日 1/13 次）；proxy 侧
  同查询 7.2s 完成，被 15s 客户端上限截断（BrokenPipeError）。根因叠加 proxy 用单线程 `HTTPServer`
  → 3 路并发查询排队。
- **重放验证**: `grep -n 'timeout=30' plugins/web/searxng/provider.py` 有输出即已重放。
- **生效面**: 插件模块进程启动时加载 → 需 gateway 重启。**已于 2026-09-11 09:47:38 自动生效**：
  `hermes-stale-code-check.timer`（每 15min）比对 gateway 启动时间 vs `hermes-agent` 树内最新变更，
  发现本文件 09:31 的改动后走 `gateway_safe_restart.py` 自动重启（重启日志：drain `active_at_start=0`，
  无会话被中断）。**不必手动安排窗口**。注：探针只监控 `~/.hermes/hermes-agent/`；
  `scripts/`、`skills/`、`memories/` 变更无需重启（子进程每次全新加载 / 每会话热读）。
- **升级注意**: 上游若把超时参数化/暴露为 config，优先采上游实现并弃本补丁。

- **上游化判定 (2026-09-27)：不重复提 PR，转「跟随上游 #110269」**。worktree `/tmp/pr-p10-modelnorm`
  （detached `origin/main` `ddb56d0aa1`）复核：
  ① 上游 `plugins/web/searxng/provider.py:30` 仍是 `timeout=15` 硬编码，`http_get_json` 的 `timeout` 是必填位置参数
     （`plugins/web/_common.py:140`），且**所有** provider 一律硬编码（brave_free 15 / ddns 10 / keenable 30 /
     firecrawl、perplexity、tavily 60）——即「硬编码」是上游约定，本补丁的 30 只是本机代理的调参。
  ② 上游已有第三方 PR **#110269「feat(web): make SearXNG HTTP search timeout configurable」**（作者 Rook-CodeVolt，
     open、0 评审、0 评论、09-13 后无互动）：新增 `web.searxng_timeout`（默认 15），改 `config_defaults.py` +
     `plugins/web/_common.py` + `searxng/provider.py` + 测试 + 文档。本机核验 `searxng_timeout` **尚未进 main**。
  ③ 结论：这正是本段「升级注意」预判的形态，但已有人在推 → 我们**同主题另开 PR 只会重复**（上游对重复 PR 通常直接关）。
  - **处置**: patch_manifest `PR_INTENT["P11"]="blocked"`（前置=等 #110269 落地），本地保持 `timeout=30` 硬编码、
    补丁保持 active；#110269 合入后按**上游键名** `web.searxng_timeout` 配置化（届时本地补丁从 core 降级为 config 项，
     可弃），若 #110269 被静默拒绝/关闭则重新评估（自行参数化或保留硬编码，理由记档）。观察项：wj 已建（含判据与停止条件）。
  - **不自行先做的理由**: 在我们本地代码里读一个**尚未合入**的上游键名，等于把本地行为绑在一个可能改名/不被接受
    的提案上；而本补丁现状（硬编码 30）已在生产生效且无害，没有必须抢跑的收益。

### P12. checkpoint 临时根跳过 + /tmp 系统残留排除（消除 40/日 rc=128 错误）— 2026-09-13

- **文件**: `tools/checkpoint_manager.py`（`_TEMP_ROOTS` + `ensure_checkpoint` 守卫；`DEFAULT_EXCLUDES`）
- **症状**: `errors.log` 每日 ~40 条 `tools.checkpoint_manager: Git command failed: git add -A (rc=128) stderr=warning: 无法打开目录 'snap-private-tmp/' ... error: 无法索引文件 'gdm3-config-err-*' / fatal: 添加文件失败`，
  长期驱动健康快照「近24h 真实错误 43 条」（阈值 20）告警。
- **根因**: 会话把临时根 `/tmp` 注册为 checkpoint 项目（写 `/tmp/xxx` 时 `get_working_dir_for_path` 回退到父目录 `/tmp`）。
  `/tmp` 含 root 属主不可读条目（`snap-private-tmp/`、`systemd-private-*/`、`.org.chromium.Chromium.*/`、`gdm3-config-err-*`）；
  `git add -A` 遇到不可读**文件**即 `fatal: 添加文件失败` → rc=128（不可读目录仅 warning）。
- **修法（两处，均最小面）**:
  1. `ensure_checkpoint`: 临时根自身（`/tmp`、`/var/tmp`、`/dev/shm`，取自 `tempfile.gettempdir()`）与 `/`、`$HOME` 并列为「过宽目录」跳过。
     **仅精确匹配根目录** —— `/tmp` 之下子目录（pytest `tmp_path` 项目、真实项目）仍照常快照，语义不缩水。
  2. `DEFAULT_EXCLUDES` 增补上述 4 类 root 属主残留模式，工作目录含它们时不再 abort。
- **被否方案（实测）**:
  - 「让 /tmp 快照成功」→ /tmp 可读载荷 ≈210MB（Chrome profile 缓存为主），store 上限 500MB → 挤掉正常项目历史，否。
  - 「`git add --ignore-errors`」→ 实测仍 rc=1 且落 ERROR 行，否。
  - 「临时根整树跳过」→ 打掉 31 个上游测试（pytest tmp_path 语义）：基线 53/53 全绿 → 该修法 31 failed，否。
- **重放验证**: `grep -q '_TEMP_ROOTS' tools/checkpoint_manager.py && grep -q 'snap-private-tmp/' tools/checkpoint_manager.py`；
  回归 `scripts/run_tests.sh tests/tools/test_checkpoint_manager.py tests/tools/test_checkpoint_path_roundtrip.py` = **57 passed / 0 failed**。
- **生效面**: checkpoint 逻辑在工具进程内即时生效（无需 gateway 重启）；`store/info/exclude` 已同步写入 54 条。
- **升级注意**: 上游若已跳过临时目录或把 exclude 列表配置化，优先采上游实现并弃本补丁。
- **✅ 复核 2026-10-11（来自 PR #118636 上 martr1a 的 finding）**: 上游新版 `get_working_dir_for_path`
  改为「注册根优先于项目标记」（`origin/main` 1396-1399：`owners = [root for root in roots
  if candidate.is_relative_to(root)]`，`roots` 取自 `_list_projects(_store_path())`）→ 若 store 里
  **已注册过宽根**（`/`、`$HOME`、临时根；多为本补丁之前的遗留），其下真实项目（如 `/tmp/project`，
  带 `.git`）会被先解析到该根、再被本补丁的 breadth guard 拒快照 —— rc=128 停了，但该项目
  **一次快照也拿不到**，唯一痕迹是 DEBUG 行（静默丢快照，比原症状更隐蔽）。
  - **本机实测不触发**: `_list_projects()` 实跑 → 6 个注册根（`.hermes/scripts`、`data/reports`、
    `skills/.../references`、`data/audit_outputs/full-audit-20261001`、
    `cache/scratch/verify166030`、`skills/devops/alerting-exposure`），逐条比对
    `{/, $HOME, _TEMP_ROOTS}` **零命中**。属**升级后**风险：本仓旧版 resolver 是纯标记回溯
    （`checkpoint_manager.py:857-866`），无 owner 优先，所以现在复现不出来。
  - **升级注意追加**: 落上游新 resolver 后，本补丁的 breadth guard 必须与新的 owner 解析对齐 ——
    若将来 store 里出现 `/`/`$HOME`/临时根，按 martr1a 的修法给 owners 加 too_broad 过滤
    （`… and str(root) not in too_broad`，too_broad = guard 用的那个集合），否则本补丁会把
    「注册根优先」放大成全局静默停摆。
  - **同点位第三方 PR #99526**（`fix(checkpoints): skip all shared system temp roots in the breadth
    guard`，author alissonrgalindo，08-31 开、10-04 仍在更新）改的是**同一 guard 的临时根集合**
    （多 macOS `/private/*` 变体 + 双向 `resolve()`）→ 与本补丁第一半（`_TEMP_ROOTS`）重叠；
    已在 #118636 回帖披露，不在我们的 PR 里重复那一半（避免「Extend, don't duplicate」被打回）。

### P13. cron 显式列出的 skill 不受 `platform_disabled` 过滤（手动 run 手册被静默跳过）— 2026-09-13

- **文件**: `tools/skills_tool.py`（`skill_view` / `_is_skill_disabled` 增 `allow_platform_disabled`）、
  `cron/scheduler_prompt.py`（`_load_cron_skill_parts` 显式传 True）、
  `tests/cron/test_cron_skill_platform_disabled_p13.py`（新增回归）
- **症状**: KB·半导体充实(19:15二四六) 手动补跑时 prompt 出现
  `The following skill(s) were listed for this job but could not be found and were skipped: knowledge-base/kb-enrichment-lite`
  → 作业手册整段缺失、降级运行。同作业的**排期**运行正常加载（09-10/09-08/09-05 加载，09-13/09-03 跳过）。
- **根因**: 手动 run 从聊天端触发（飞书会话）→ 会话环境 `HERMES_SESSION_PLATFORM=feishu`；
  `kb-enrichment-lite` ∈ `skills.platform_disabled.feishu`（当初为交互路由瘦身而禁）→
  `skill_view` 走 `_is_skill_disabled(resolved_name)` 判 disabled → cron 侧按「skill 未找到」跳过。
  platform_disabled 的语义是「不进交互路由」，不应拦「作业显式列出」的加载。
- **修法（最小面，默认零行为变化）**: `allow_platform_disabled=True` 跳过**平台档**，但**仍**尊重
  config 全局 `skills.disabled`（废弃技能不得被显式加载复活）；`skill_view` 默认 False → 交互路径不变。
- **被否方案**: ① 把 `kb-enrichment-lite` 从 `platform_disabled` 移除 —— 纯 cron 手册会进聊天端路由，
  丢掉当初瘦身意图；② 改 4 个作业的 skills 指向完整版 `knowledge-base-enrichment` —— token 注入显著变大，
  lite 手册存在的意义就是降 token。
- **重放验证**: `grep -q 'allow_platform_disabled: bool = False' tools/skills_tool.py && grep -q 'allow_platform_disabled=True' cron/scheduler_prompt.py`；
  回归 11 个相关测试文件 = **111 passed / 1 skipped**（含 T1 交互仍拦截 / T2 显式加载通过 /
  T3 全局 disabled 仍拦截 / T4 `_load_cron_skill_parts` 端到端注入手册）；
  实机（会话平台=feishu）用真实作业定义调用 = 两份手册均载入、无 skip 提示。
- **升级注意**: 上游若给 cron skill 加载提供同类开关，或重构 `_load_cron_skill_parts` 签名，以本段为准对齐；
  `_is_skill_disabled` 与 `agent/skill_utils.get_disabled_skill_names` 需保持同步（全局档语义）。
- **上游化进度 (2026-10-11 rebase 完成)**: PR #117143 旧头 `ed0730cb96` 与 `origin/main` 冲突（单文件
  `tools/skills_tool.py`），已 rebase 到新 base 并 force-push（lease 比对）→ 新头
  `b368d0c86a6ad7a3504c42588956cd8090354f49`，API 回读 `mergeable: true`（`mergeable_state=blocked`＝等
  检查/批准）。冲突实质：上游把 `_is_skill_disabled` 改为 `*names` 变参，且调用点传 owned-relative `rel`
  （同层重名副本那一行）——本 flag 必须**并行穿过两处调用**，否则重名副本那一支仍被平台档拦掉：
  `platform_gate = platform_disabled is not None and not allow_platform_disabled`。
  同日复现 T4 类坑（上游测试窄仿冒签名）：`tests/agent/test_skill_commands.py::TestDuplicateNamesAgreeAcrossSurfaces`
  用 `lambda *n, platform=None` 替换 `_is_skill_disabled`，新增 kwarg 使其 `TypeError`，而 `skill_view` 的兜底
  `except Exception` 把它吞成「无 content 的错误信封」→ 表现为 `KeyError: 'content'`。**故本次 PR 的第二提交
  同时把该 fake 放宽为 `**kw`**（改动仅签名一行，已披露）。
  证据（新 base，2 文件）：82 passed / 19 failed，19 项与 pristine `main` **同一失败集合**（本机解释器位于
  hermes home 下触发的环境型失败，非本次改动）；红态（仅回退实现三文件、保留测试）76 passed / 25 failed，
  多出的 6 = 5 个新用例 + `TestBuildJobPromptAbsoluteSkillPath::test_absolute_skill_path_normalized_before_skill_view`；
  邻接 7 文件（所有 fake `skill_view`/`_is_skill_disabled` 的测试 + skills view 系列）114 passed / 11 failed，两树同集。
  **合入后**：删本地 hunk → 本段改「跟随上游」。

### T1. 上游测试断言与本地语义对齐（移植必带）— 2026-09-10

- **文件**: `tests/cron/test_agent_scheduling_gate.py`、`tests/tools/test_web_providers.py`
- **背景**: 上游测试断言的是**纯上游语义**，与本仓两处本地语义/新增插件冲突，重放或合并上游后必红：
  1. `_resolve_cron_disabled_toolsets`（cron/scheduler.py:392）受本地补丁 0002（2026-08-23，cron agents
     `skip_memory=True`）影响无条件加入 `memory`，而上游 4 个用例硬编码
     `["cronjob","messaging","clarify"]`。处理：期望列表补 `"memory"`；
     `test_memory_not_denied` → `test_memory_denied_by_local_override`（改反向断言 + 注释指向本台账）。
  2. `plugins/web/bocha`（P8）经运行时插件发现路径被注册；用例 `_clear_web_creds` 只做 `delenv`，
     而 bocha `_load_api_key()` 是**直读 `~/.hermes/.env` 文件**、不经 `os.environ` → 在有真密钥的机器上
     `is_available()` 仍为真 → `web_search_registry._resolve()` 落到「唯一可用 capable provider」分支
     → 真实调用博查并返回 `Bocha daily quota exhausted (61/60)`，与「零凭据安装返回 error 信封」断言冲突。
     处理：`_clear_web_creds` 末尾追加
     `monkeypatch.setattr(_bocha_provider, "_load_api_key", lambda: None)`（ImportError 时跳过）。
- **判定依据**: 纯净基线 `2237be3559` 跑同样文件 = 全绿；移植树用 `HOME=/tmp/empty-home`（无 .env）跑
  = 全绿 → 属**环境依赖型测试缺陷**，非产品缺陷、非 P8 代码 bug。
- **生产影响**: 无。`config.yaml` 显式 `web.backend: searxng` / `web.search_backend: searxng`
  → `_resolve()` 规则 1「显式配置优先」生效，bocha 不会被选中，不消耗
  `~/.hermes/data/.bocha_daily.json` 共享日配额（60/天，与 source_manager 同源）。
- **回归结果**: `tests/cron` = **1 failed / 1230 passed**（该 1 项
  `test_mirror_origin_fallback.py::TestFallbackMirrorEndToEnd::test_all_broadcast_does_not_mirror`
  在纯净 v0.21.1 上同样红，上游固有）→ 与基线**零差集**。
- **升级注意**: 上游若改动这两处断言（cron memory denylist / provider 选择顺序），以本段为准重新对齐；
  P8 插件若日后正式启用（`plugins.enabled` + `web.search_backend: bocha`），本测试的 monkeypatch 需复核。

### T2. 上游测试 stub 与本地 `run_with_bounded_executor_shutdown` 对齐 — 2026-09-10

- **文件**: `tests/hermes_cli/test_gateway.py`（2 处 `fake_run` stub：POSIX 退出码契约测试 + Windows-only diag 测试）
- **背景**: 本地补丁使 `hermes_cli/gateway.py` 的 `run_gateway()` 执行
  `from gateway.run import start_gateway, run_with_bounded_executor_shutdown`（P7 有界 executor join）。
  上游两处测试把 `sys.modules["gateway.run"]` 替换为只含 `start_gateway` / `_exit_after_graceful_shutdown`
  的假模块 → 该 import 抛 `ImportError: cannot import name ... (unknown location)` → 子进程 exit 1。
  后果：`test_gateway_run_subprocess_preserves_daemon_exit_codes` 4 个参数中 `failure`（期望 1）
  因 ImportError 恰好蒙对，`systemexit:75` ×2 / `systemexit:78` 3 个全红 →
  **该测试对本地代码零覆盖**：真正要守的 75（systemd restart 交接）/ 78（致命配置错误）退出码契约从未被执行。
- **处理**: 两处 stub 追加 `fake_run.run_with_bounded_executor_shutdown = asyncio.run`
  （真实 helper = 跑协程 + 有界 join default executor，`asyncio.run` 为忠实最小桩）。
- **判定依据**: 生产树（v0.21.0 + 本地补丁）**同样 3 failed**（逐条同名）→ 属**存量**，非 P6–P10 引入。
- **结果**: 移植树 `tests/hermes_cli/test_gateway.py` = **37 passed / 3 skipped**，与纯净基线 `2237be3559` 一致；
  且 75/78 退出码契约首次真正被验证（通过）。
- **升级注意**: 上游测试若重构 stub 需保留该符号；Windows-only 分支本机不可验（Linux），该处改动为纯追加。

### T3. 上游 bundled web-plugin 名单加 `bocha`（移植必带）— 2026-09-10

- **文件**: `tests/plugins/web/test_web_search_provider_plugins.py`（3 处白名单）
- **背景**: P8 将 `plugins/web/bocha/`（博查搜索）纳入版本管理，上游 v0.21.1 无此 provider。
  上游三处断言把它当"全部 bundled 插件"的**精确集合**校验：`test_all_bundled_plugins_present_in_registry`
  （`names == [...]`，10 项）、`test_capability_flags_match_spec`（parametrize）、
  `test_each_plugin_has_name_and_display_name`（parametrize）→ 移植树 1 failed
  （`At index 0 diff: 'bocha' != 'brave-free'`），基线 `2237be3559` 34 passed 全绿。
  **这是逐文件全量回归（v5+v6+v7 共 2,994 文件）抓到的唯一真·移植引入红**，由 v7 自带基线对照自动暴露。
- **处理**: 三处名单加 `"bocha"`；能力用例补 `("bocha", True, False)`
  （`provider.py`：`supports_search=True` / `supports_extract=False`）。
- **有意不加**: `TestIsAvailable` 类 —— 断言"无 env 配置时 `is_available()` 为 False"，
  而本地 bocha `_load_api_key()` **直读 `~/.hermes/.env` 文件**（同 T1 机制），本机有真密钥 → 加了必红；
  该断言在"文件侧密钥"语义下本身不成立，不加即不引入假红。
- **结果**: 移植树 `36 passed`（34 + 2 新增 param）；基线 `34 passed`。
- **升级注意**: 上游若扩充/重排该名单须保留 `"bocha"`；若上游自行引入 bocha，删本项并复核 provider 名一致。

### T4. 上游测试 fake 签名与本地 P13 kwarg 对齐（移植必带）— 2026-09-25

- **文件**: `tests/cron/test_scheduler.py`（`TestBuildJobPromptAbsoluteSkillPath` /
  `TestBuildJobPromptBumpUse` 里的 `_skill_view` fake）
- **症状**: `bash scripts/run_tests.sh tests/cron/` 2 例失败 —— `seen_names == []`，日志
  `skill '…' returned invalid JSON, skipping`。**假红/假绿都不对**：这不是功能坏了，
  是上游 fake 的签名落后于本地语义。
- **根因**: 本地 P13 让 `_load_cron_skill_parts` 调 `skill_view(name, allow_platform_disabled=True)`，
  而上游 fake 只接受 1 个位置参数 → `TypeError` 被
  `except (json.JSONDecodeError, TypeError)` 吞掉 → 判成「无效 JSON」跳过。异常面被吞是本例
  最难查的点：症状指向 skill 未找到，真因是测试替身签名。
- **修法**: fake 补 `allow_platform_disabled: bool = False`，并在绝对路径用例里
  **断言实参为 True**（把 P13 的语义固化成行为契约，而非仅让测试变绿）。
- **结果**: `bash scripts/run_tests.sh tests/cron/` = **1252 passed / 0 failed / 2 skipped**（101 文件）。
- **升级注意**: 上游若重写这两个 fake 或给 `skill_view` 换签名，重放后先跑 `tests/cron/`；
  若上游 fake 已接受该 kwarg，则删本项。**禁止**用 `except TypeError` 回退调用绕开（会把
  P13 静默降级 + 吞真异常）。

### T5. v2026.9.24 新测试与本地 P5/P7/P13 契约对齐 + 本树独有目录声明（移植必带）— 2026-10-01

- **文件**（5 个上游测试文件 + 6 个空目录清理）: `tests/agent/test_prompt_cache_boundary.py`、
  `tests/gateway/test_feishu.py`、`tests/gateway/test_feishu_profile_scope_hops.py`、
  `tests/test_tests_tree_layout.py`、`tests/test_managed_runtime_resolution.py`
- **来源**: 2026-09-30 切 v2026.9.24 后逐文件全量回归（`/tmp/regress924b-all.log`），
  `.regressions` 报 7 项【真回归候选】（本树 rc=1 / 基线 rc=0）。逐条归因后**全部是
  carried-patch 契约落后 + 重放残留，零产品缺陷**：
  1. `test_prompt_cache_boundary.py::TestCronBuilder`（9.24 新增用例类）：fake `_skill_view`
     只收 1 个位置参数，被 P13 的 `allow_platform_disabled=True` 打成 `TypeError`，
     又被 `except (json.JSONDecodeError, TypeError)` 吞成「returned invalid JSON, skipping」。
     修法沿用 T4：补形参 `allow_platform_disabled: bool = False` **并断言为 True**（钉住 P13 契约）。
  2. `test_feishu.py::…uses_owned_executor`：断言「恰好一次提交」→ P7（WS 客户端进自有池）
     + P5（假活守卫的握手确认 `conn_evt.wait` 也进自有池）合计两次提交。改为
     「提交非空且全部 = 自有池」。
  3. `test_feishu_profile_scope_hops.py`：`SimpleNamespace` 桩缺 P5/P7 契约 —— 补
     `stub._get_ws_executor = …_get_ws_executor.__get__(stub)`、`_ws_connected_event=None`，
     收尾改为「只关实际创建过的池」（P7 后 `_get_sdk_executor` 可能根本未被调用）。
  4. `test_tests_tree_layout.py`：`_NON_MIRROR_DIRS` 加 `local`（`tests/local/` 是本树独有横切
     套件，上游无此目录）。
  5. `test_managed_runtime_resolution.py`：`_EXEMPT_DIRS` 加 `.hermes-runtime`（宿主托管 CPython
     runtime，gitignore 的构建产物，与 `.venv`/`venv` 同类；不排除则扫描进 stdlib `webbrowser.py`
     与 pip `entrypoints.py` 的 `bare_which`）。
  6. `test_plugin_discovery.py` + `test_tests_tree_layout.py` 的另一半根因是**空目录残留**：
     重放删文件未删目录 → `plugins/model-providers/opencode-free`、
     `tests/{acp,cli,relay,run_agent,state}` 共 6 个空目录（双向无内容、git 不跟踪、无引用，已删）。
  7. `test_completed_process_results.py`：复跑即绿，判定为当次运行的假阳性（顺序/时序敏感）。
- **判定依据**: 基线树 rc=0 且上游 v2026.9.24 中 `_ws_connected_event` / `_get_ws_executor`
  命中数均为 0（`git show v2026.9.24:plugins/platforms/feishu/adapter.py`）→ 两处确为本地
  carried patch（P5 / P7），非上游行为。
- **结果**: 7 个文件复跑全绿（21 / 76 / 2 / 3 / 4 / 2 / 2 passed）；提交 `25c3935065`。
- **升级注意**: ① 重放脚本必须补「删文件后清空目录」步骤，否则 2 个用例必红；
  ② 若上游 `skill_view` 已接受 `allow_platform_disabled` 或上游自行引入自有 WS 池，删本项；
  ③ 禁止用 `**kwargs` / `except TypeError` 回退把 2、3 两项「静默容忍」—— 那会把 P7/P13 的
  契约降级成无人看守。

### P14. bocha provider 计量口径补全（results 字段 + 跨月保留）— 2026-09-16 建立

- **文件**: `plugins/web/bocha/provider.py`（`_load_quota` / `_fresh_quota` / `search`）
- **背景**: Bocha 官方按**返回结果条数**计费（实测本地条数求和 vs 官方 calls 误差 0.17%），
  但本地三处计量都不完整：① 脚本侧 `scripts/bocha_search.py` 跨日整块重置 → `monthly_count` 每天归零
  （与 2026-09-15 插件修过的同一 bug，脚本侧漏修）② 插件/脚本只记调用数、不记条数
  ③ 看门狗只读 proxy 计数 → 余量推算漏掉采集层一半以上消耗（09-16 误报「≈32 天」）。
- **处理**: 插件与脚本侧统一新增 `results` / `monthly_results` 字段；跨日只重置当日字段、
  跨月才归零月字段；`search()` 返回时按 `len(web_results)` 累加条数。同源文件
  `~/.hermes/data/.bocha_daily.json`（与 `source_manager` 共享，日 60 / 月 1,000 上限仅告警不阻断）。
- **验证**: 临时目录单测（跨日保留 / 跨月归零 / 条数累加 / 原子写无 tmp 残留）全通过；
  生产计数文件未触碰（mtime + 内容比对）。
- **升级注意**: 上游 provider 若重写配额逻辑，保留「results 口径 + 月字段跨日保留」两条语义；
  上游若自行引入条数计量，删本项并复核字段名（`monthly_results`）一致。看门狗
  `scripts/bocha_balance_watch.py` 依赖该字段名求和。

### P15. cron 落库侧 rc 分流（rc 2 业务告警不再写成 error / 不再累加 failure_streak）— 2026-09-18 建立

- **文件**: `cron/jobs.py`（`ALERT_EXIT_CODE` / `script_exit_code` / `_derived_last_status` /
  `_record_run_outcome` / `_job_is_stale_error_recurring`）、`hermes_cli/cron.py`
  （`_last_run_display` / `_cron_doctor_issues_for_job`）、`tools/cronjob_tools.py`（`_run_claimed_job`）
- **问题（待办 476075）**: 落库侧 `last_status` 只有二值 —— rc 2（契约 SSOT
  `~/.hermes/scripts/cron_exit_codes.py` 的 EXIT_ALERT = 业务告警：脚本已正常跑完并已把检测到的
  问题告知用户）被写成 `last_status="error"` 并计入 `failure_streak`。消费侧虽能按 rc 分流，但落库
  值本身要靠重新解析 last_error 文本才能分辨失败/告警，且 rc 2 会把 streak 推到触发「failed N runs」
  劝阻的阈值。实测 2026-09-18：「管线·告警守护」streak=4、「系统·代码健康扫描」streak=2 全是 rc 2 虚记。
- **修复**: 写 last_status 前解析 scheduler 的权威错误头（`Script exited with code N` / `脚本退出码 N`，
  仅前 3 行；不全文宽松匹配，避免把 stdout 里子进程的 exit=0 当成本脚本退出码）→ rc 2 落 `alert`、
  rc 0 落 `ok`、其余/解析不出落 `error`（fail-safe）。`failure_streak` 只在 `error` 累加，`alert`
  与非失败态一致清零；显式 `status` 参数（`blocked_config` 等）仍优先。**仅对新写入生效**，不批量
  改写历史 `error` 记录；关机打断（interrupted）无 rc 仍写 `error`，契约的打断判定不受影响。
- **消费侧配套**（不在本仓库，但与本补丁同批）: `~/.hermes/scripts/cron_exit_codes.py` 的
  `classify_job_state` 认 `alert` 状态并新增 `TRIAGE_STATUSES = ("error", "alert")`（两代落库口径的
  前置过滤）；8 个消费脚本把 `last_status == "error"` 改成 `in TRIAGE_STATUSES`；面板
  `hermes-panel/app/cron_health.py` 同样补 alert 前置分支 —— 否则新落库的 alert 记录会从
  「失败 / 告警」两个清单里同时消失（契约纪律 2 告警失联）。
- **验证**: `scripts/run_tests.sh tests/cron/test_jobs.py tests/cron/test_run_one_job.py
  tests/hermes_cli/test_cron.py` → 改前 149 passed / 改后 167 passed / 0 failed；真实 jobs.json 记录
  回放（HEAD 版 vs 工作区版同输入对比）见提交说明；`tests/cron/` 全量只有 2 个 `test_scheduler.py`
  技能提示用例失败（HEAD 上同样失败，与本次无关）。
- **升级注意**: 上游若重写 `_record_run_outcome` 的 last_status 计算，必须保留「rc 2 → alert」与
  「failure_streak 只对 error 累加」两条语义；`ALERT_EXIT_CODE = 2` 必须与
  `~/.hermes/scripts/cron_exit_codes.EXIT_ALERT` 保持一致（改契约要同步两侧）。

- **上游化判定 (2026-09-27)：不做上游化，改判 `local-policy`（移出 PR 候选）**。worktree `/tmp/pr-p10-modelnorm`
  （detached `origin/main` `ddb56d0aa1`）复核：本补丁段**红**（上游 `cron/jobs.py:2340` 仍是二值 `last_status`
  `status or ("error" if not success else …)`、无 rc 解析、无 alert 态，`cron/constants.py` 无退出码常量），
  但红的性质是「上游**故意**不这么做」——
  ① 上游契约是两态且**明文文档化**：`website/docs/guides/cron-script-only.md:140-150` 表格 —— rc0+非空 stdout
     = 原样投递；**rc0+空 stdout = 静默 tick（文档明说这是看门狗的标准做法）**；rc≠0 = 投递 error alert
     （原文 "so a broken watchdog doesn't fail silently"）。即「脚本正常跑完、扫到问题就说话」在上游一律用
     **rc0 + stdout** 表达，无需第三态。
  ② 本仓第三态（rc 2 = 业务告警）服务的是**本地记账**：本仓失败率口径、`.cron_error_log.json`、消费侧 8 脚本、
     面板；其 SSOT `~/.hermes/scripts/cron_exit_codes.py` 在**仓库外**，上游 PR 无从引用，只能把
     `ALERT_EXIT_CODE = 2` 硬编码进 `cron/jobs.py` —— 而 rc 2 在上游语境里是常见**错误码**（`hermes update`
     在镜像托管安装上拒绝时就是退出码 2、bash 误用也是 2）→ 会把真失败静默降级成「告警」，**违反上游契约**。
  ③ 选项与后果：(A) 判本地口径、不上游化（**采用**；core 仍 19，本地补丁升级窗口继续重放）；
     (B) 上游化 opt-in 配置 `cron.alert_exit_codes`（默认空 → 上游行为零变化；合入后本地设 `[2]`、P15 可退役出 core；
     属**新特性提案**而非补丁移植，体量更大，且维护者可能回「用 rc0+stdout 就够」而关闭）；
     (C) 原样硬编码 rc 2（体量最小，但违反上游文档化契约，预期被驳回或要求改造）。
  - **落地**: patch_manifest `category` 改 `local-policy`、`pr_candidate=False`、移出 `PR_INTENT`；
    **本地代码不动、补丁保持 active**。09-27 就此询问用户未获回复（超时），按推荐项 (A) 先落地 ——
    **可随时改选 (B)**（改选只需恢复 `category=upstreamable` / `pr_candidate=True` 并按 ③(B) 开 PR）。
    若日后上游自行引入告警态语义，优先采上游实现并弃本项（与本段「升级注意」同义）。

### M1. cron agents 禁用 memory（恢复 v0.20.4 行为）— 2026-08-23 建 / 09-01 重放

- **文件**: `cron/scheduler.py`（`_resolve_cron_disabled_toolsets` 无条件加入 `memory`）
- **来源**: commit `e7b969131`（08-24 reset 丢失），09-01 v0.21.0 升级随补丁集重放（`2a527c9fc`）
- **理由**: cron agent 无会话记忆语义，memory 工具集进 cron 会污染长期记忆
- **重放验证**: `grep -qE 'skip_memory' cron/scheduler.py`（verify_patches「M1注入点」）
- **派生**: T1（上游 `tests/cron/test_agent_scheduling_gate.py` 4 用例硬编码三元列表，需补 `memory`）
- **升级注意**: 上游若把 cron 工具集解析改为可配置，优先采上游实现并弃本项

### M2. FEISHU_BOT_MENTION_MAP 出站 bot→bot 真实提及 — 2026-08-23 建 / 09-01 重放

- **文件**: `plugins/platforms/feishu/adapter.py`（出站 mention 渲染，4 处）
- **来源**: commit `6194c3dad`（08-24 reset 丢失），09-01 重放（`2a527c9fc`）
- **理由**: 跨实例协同群 @对方 bot 需真实 mention（否则对方收不到事件，见 USER.md 跨实例分工）
- **重放验证**: `grep -q 'FEISHU_BOT_MENTION_MAP' plugins/platforms/feishu/adapter.py`（verify_patches「M2注入点」）
- **升级注意**: 上游若提供 outbound mention 映射能力，优先采上游并改本项为 PR 候选
- **✅ 上游化 2026-09-27（判红 → 已开 PR）**: 上游 adapter 无出站提及能力（无 `_extract_mentions`/`at` 元素，
  `_build_markdown_post_payload` 只发 `md`）→ 症状成立。已在 `/tmp/pr-m2-botmention`（detached origin/main
  `3d7058fc83`）移植并开 **PR #124952**（`feat/feishu-outbound-bot-mentions`，commit `098c084c93`）：
  新增 config 键 `feishu.bot_mention_map`（mapping，按 profile 灌 `extra`，不进进程 env）+ env 同名
  `FEISHU_BOT_MENTION_MAP="Name=ou_x,Other=ou_y"`；`_extract_mentions` 把已配置的 `@名` 换成 `@_mention_N`
  占位（最长名优先 + 词边界），post 渲染成 `{"tag":"at","user_id":<open_id>}`；逐 chunk 提取（避免占位被截断
  成字面量）；无图配置时渲染逐字节不变。红：`origin/main` 回退 adapter 后同一测试 **22 failed / 1 passed**；
  绿：**23 passed**；回归 `tests/gateway/test_feishu*.py` 两树失败集相同（184 vs 161 passed，+1 文件 +23 例）。
  **#124952 合入前本地补丁保持 active**；合入后按本仓 config 键名对齐并退役本项。
- **上游化进度 (2026-10-11 rebase 完成)**: PR #124952 旧头 `e5581a8710` 与 `origin/main` 冲突已解并 force-push
  （lease 比对）→ 新头 `e162d657d25f...`，API 回读 `mergeable: true`。冲突仅 `_build_markdown_post_rows` 签名形式
  （上游已把注解现代化为 `list[list[dict[str, str]]]`，本 PR 增 `mention_ids` kwarg）→ 保留上游注解 + 新 kwarg，行为不动。
  证据（新 base，10 个 gateway feishu 文件含本 PR 新模块）：**147 passed / 1 failed**；同批在 pristine `main`（9 文件，
  无新模块）**121 passed / 1 failed** —— 那 1 条 `test_feishu_comment.py::TestResolveModelAndRuntime::
  test_configured_reasoning_reaches_the_comment_agent` **两树同**，属既有环境型；红态（仅回退 adapter、保留新模块）
  **1 passed / 25 failed**（26 例里 25 例靠本改动才过，覆盖面确为承重）。
  全量邻接复核（2026-10-11 补跑，上表未含的其余 8 个 upstream feishu 模块：bot_admission / bot_auth_bypass /
  channel_prompts / comment_rules / lazy_import / meeting_invite / onboard / ws_multiplex_isolation）：PR 树与
  pristine `87da183a8a` 各 **8 files / 51 passed / 2 failed**，失败集**零差集**（`test_feishu_bot_auth_bypass.py`
  2 条两树同）。两批合计 = upstream base 上全部 **17 个** feishu 测试模块均覆盖，无一例本 PR 引入。
  （`test_feishu_thread_loop_proxy.py` 仅存在于本仓 local-patches，upstream base 无此文件，不在邻接面内。）

#### P14 补记：登记缺口（2026-09-22 入场闸门首次运行发现）

> 2026-10-01（审计 E2-07）：本节原先误用 `### P14.` 标题**与上面主条重复登记**，使
> verify_patches.sh 新增的「登记面去重」检查报红；已降级为补记（不再计入 id 集合），
> 内容原样保留。

- **文件**: `plugins/web/bocha/provider.py`（`_load_quota` / `_fresh_quota` / `search`）+
  仓库外同口径副本 `~/.hermes/scripts/bocha_search.py`
- **背景/修法/验证**: 见本表下方 2026-09-16 原文段（commit `c5f76f54a5` + 清警告 `ccabdab5fc`）；
  官方按「返回条数」计费，跨日只重置当日字段、跨月才归零月字段
- **⚠️ 登记缺口（2026-09-22 入场闸门首次运行发现）**: 本项自建立起未被登记进 `verify_patches.sh`
  的 `for pat in` 清单，也无注入点检查 → 18 天内「补丁消失/取值回退」零检出。
  本次补齐两条检查（插件侧 + scripts 侧，同口径每副本一条）：`P14注入点1` / `P14注入点2`
- **升级注意**: 上游 provider 若重写配额逻辑，保留「results 口径 + 月字段跨日保留」两条语义；
  看门狗 `scripts/bocha_balance_watch.py` 依赖字段名 `monthly_results`

### P16. work journal 三件套（A1 注入 + B1 todo 桥 + brief 排序）— 2026-08-21 建 / 08-29 恢复 / 09-21 迭代

- **文件**: `agent/system_prompt.py`（`_build_work_journal_block` + volatile 注入 + `_journal_due_delta`）、
  `agent/agent_init.py`（`work_journal_inject` / `work_journal_max_items` / `todo_mirror_to_journal` 三项门控）、
  `agent/inline_tool_executors.py`（`mirror_builtin_todos` 调用点）、`agent/todo_journal_bridge.py`（新模块，
  上游无同名文件）+ 测试 `tests/agent/test_work_journal_brief.py`、`tests/agent/test_todo_journal_bridge.py`
- **背景**: 08-24 reset 事故丢失、08-29 从 `4653e87fd` 提取重放（`ee47fb14a`），曾静默失效数日；
  09-21 迭代「按 进行中→到期日近者 排序取前 N」+ 缺年 `MM-DD` 支持（`ec4085ed3a`/`3c21a4e324`/`41951d8303`）
- **⚠️ 分派点搬迁（v0.21.1）**: todo 分派已迁到 `agent/inline_tool_executors.py`，旧 `agent_runtime_helpers.py`
  的 `invoke_tool` 分支为**死代码** → 注入点检查盯 inline_tool_executors（见 git-patch-replay 陷阱 11）
- **重放验证**: `verify_patches.sh` 的 `P16注入点1/2/3`
- **2026-10-18 迭代（注入块形状）**: `_build_work_journal_block` 增四项 —
  ① 顶部「📌 截至 <时点>」+ `wj-handoff` 交接入口（原块无时点，无法判快照年龄）；
  ② `kind=decision` 行尾标 `⚖️决策`（原与执行项同形，603482/926304 是等用户拍板的）；
  ③ 表外行改「表外到期/逾期 N 条（逾期 a · 今日 b）」——原「表外已逾期」把 d==0 也算逾期
  （实测 14 = 5 真逾期 + 9 今日）；④ id 上限 8 → 20（原第 9 条起无入口，实测藏 6 条含 2 条决策）。
  同步测试 `tests/agent/test_work_journal_overdue_line.py`（新增 4 例，锁决策标注/拆分口径/不截断/时点）。
  **重放要点**: 除函数体，`verify_patches.sh` 的 `P16注入点4` 会检查 `表外到期/逾期` 与 `⚖️决策` 两个标记。
- **升级注意**: 上游提供 `plugins/hooks` 扩展点；本项属本站特性，评估改走 hook 以永久退出 core
  （可一次消掉 system_prompt / agent_init / inline_tool_executors 三个高频文件的补丁面）

### P17. session_search 覆盖面对账（取消 limit 截断）— 2026-09-19 建立

- **文件**: `tools/session_search_tool.py`（payload 改为 `lineages_matched` / `raw_rows_scanned` /
  `scan_window_truncated`，取消 `limit` 截断；commit `c1941e846f`，待办 135713）
- **背景**: 旧口径按 `limit` 截断会把「已扫到但被截掉」的行报成不存在，覆盖面判读失真
- **⚠️ 登记缺口（2026-09-22 入场闸门首次运行发现）**: `CUSTOM_PATCHES.md` 与 `LOCAL_CORE_CHANGES.md`
  两份台账均未登记 → 下次升级必漏（本次补登记 + 在 `verify_patches.sh` 落两条检查）
- **重放验证**: `P17注入点1`（对账字段）/ `P17注入点2`（`scan_window_truncated` 判据）
- **升级注意**: 上游 `session_search` 若自行引入覆盖面/窗口语义，先读源码复核语义等价再决定弃留；
  属上游通用改进，列 PR 候选
- **✅ 复核 2026-10-11（错映射更正 + 静默拒绝判定）**:
  - **错映射（当日修正）**: 本项原先在 `patch_manifest.PR_LINKS["P17"]` 下挂了 **#117462**，实为
    `fix(compression): anchor ledger misses tool-call arguments and starves cheap id sections`
    （改 `agent/context_compressor.py` + `tests/agent/test_context_compressor_anchor_harvest.py`，
    **本仓无对应本地补丁** = B 路径）→ 已移入 `data/upstream_prs.json`（source=extra，巡检不中断）。
    危害：check 的 G4 把「压缩锚点 PR 静默 20 天」报成 P17 的静默风险，掩盖本条真实状态。
  - **#117126（本项）**: 09-20 开、满 20 天**零外部互动**（我方 09-26 已 bump 一次）→ 按跟踪条 125003
    的「无反馈 14 天视为静默拒绝」判 **静默拒绝**、停止再催；**决策日 2026-10-25**（届时仍零互动则关
    本方 PR 并评估下一批候选）。本地补丁保持 active —— 本仓需要该覆盖面口径，且 `origin/main` 实测
    仍无 `lineages_matched`（10-09 树）。

### P18. desktop 懒/未持久化 resume 的 info 补 desktop_contract — 2026-09-25 建立

- **文件**: `tui_gateway/methods_session.py`（`_resume_live_unpersisted` 的内联 `info` 增
  `"desktop_contract": DESKTOP_BACKEND_CONTRACT`，与上游既有 create / activate 两处修法一致；待办 120744）
- **背景**: 懒/未持久化 resume 分支返回的 `info` 缺契约号 → 客户端比对失败，误报「后端早于桌面构建」
  （笔记本 0.21.0 / 要求契约 6 复现）。上游 0.21.1 / main 同缺 → 上游 PR 候选
- **已验红**: `tests/test_tui_gateway_server.py:5573` 临时加断言 → `KeyError: 'desktop_contract'`（测试文件已还原）
- **重放验证**: `verify_patches.sh` 的 `P18注入点1`
- **升级注意**: 上游若统一 info 构造（create / activate / resume 一处出）本项自动失效；重放前先读该函数
- **✅ 复核 2026-09-27（判绿 → 不开 PR）**: 上游已修 → commit `35ade5719d`
  `fix(tui_gateway): report desktop_contract in all lazy session-info shapes`（2026-09-16，已是 `origin/main`
  祖先，标题即覆盖本项症状面）。实证：`origin/main` 的 `_resume_live_unpersisted` info 内已含
  `"desktop_contract": DESKTOP_BACKEND_CONTRACT`（`tui_gateway/methods_session.py` 689）。**本地补丁暂留
  active**（本仓仍在旧谱系），升级窗口落该提交后退役（`category→deprecated` + `active=False`）；退役前重放时
  先读该函数确认本地不再多补一处。
- **生效条件**: 改的是 `tui_gateway`（桌面后端与网关共用）→ 需重启后端进程才生效（本次未重启，见待办 120744）

### P19. rc=2 业务告警写入端闸门 + 重复抑制（不再入事件台账） — 2026-10-09 建立

- **文件**: `cron/scheduler.py`（`_is_business_alert` / `_alert_repeat_suppress_hours` /
  `_compose_business_alert_delivery` + `_compose_run_delivery` 增一条 `elif` 分支）、`cron/alert_cooldown.py`（新）、
  `cron/scheduler_failure_copy.py`（`business_alert_notice`）、`tests/cron/test_cron_business_alert.py`（新，18 用例）
- **背景**: 契约 SSOT = `~/.hermes/scripts/cron_exit_codes.py`：rc=2（`EXIT_ALERT`）表示**脚本跑完并已把发现
  告知用户**，不是失败。但写入端 `scheduler.py::_upsert_incident_for_failure` 对任何非 success 的跑次无条件
  建/更新 incident → rc=2 也照记，台账只增不减，ack 永远追不上（周检脚本自注同样结论，见待办 853482）。
  实测 `cron/executions.db`→`cron_incidents` 117 行中 **rc=2 = 68 行（58%）**。
- **本次改动**: ① rc=2 走独立分支 → **不建 incident**（rc=1 / rc=7 / 解析不出仍走失败路径，fail-safe）；
  ② 投递照旧（契约纪律 2：告警必须仍被看见）但换业务告警文案（明说「不是失败」）；③ 同一 `(job, 规范化文本)`
  在 `cron.alert_repeat_suppress_hours`（默认 6h，`0`=关闭）窗口内不重复投递，抑制写
  `data/alert_cooldown_suppressed.jsonl` 可审计；冷却存储 30 天老化 + 500 键上限 + flock/原子写
  （不会无界增长，失败一律 fail-open 照常投递）；④ 治理周报 rc=2 列数据源改为 `executions`（防该格静默变 0）
  + 增「冷却抑制 N 条」。
- **已验红**: 上游行为下 rc=2 必进 `cron_incidents`（`tests/cron/test_cron_incidents.py` 覆盖写入端）；
  本项新增 18 用例含负向对照（rc=1/rc=7/非权威头形状/空文本 → 必须仍走失败路径）。
- **重放验证**: `verify_patches.sh` 的 `P19注入点1`（`_is_business_alert`）/ `P19注入点2`（冷却窗口）/
  `P19注入点3`（`business_alert_notice`）
- **升级注意**: 上游若自行把 rc=2 从 incident 写入排除（或改成独立告警通道），本项与上游语义合流 →
  先读 `_compose_run_delivery` 与 `_upsert_incident_for_failure` 再决定弃留；上游 `cron/scheduler.py`
  大改时需按 `git show` 3way 重放
- **回滚**: `git checkout -- cron/scheduler.py cron/scheduler_failure_copy.py` + 删 `cron/alert_cooldown.py`
  即恢复无条件建 incident；运行时无需改数据（旧 rc=2 条目按原样留档）
- **生效条件**: 改的是调度器本体 → 需重启 cron/scheduler 进程；未重启前行为不变（新旧并存安全）

#### P19 追加（2026-10-12）：同族缺陷② —— `failure_type` 按权威退出码分流 + 崩溃路径第二道门禁

- **文件**: `cron/incidents.py`（新 `_authoritative_exit_code` / `_alert_exit_code` + `_classify_failure_type`
  改为退出码优先）、`cron/scheduler.py`（`_deliver_crash_failure` 增加 rc=2 门禁）、
  `tests/cron/test_cron_incidents_rc2_gate.py`（新，10 用例，unittest.TestCase）
- **背景**: ① 写入端 rc=2 门禁（上面 P19 本体）只覆盖了 `_compose_run_delivery` 失败分支，第二写入点
  `_deliver_crash_failure` 仍无条件 `_upsert_incident_for_failure`；② 台账分类端 `_FAILURE_TYPE_ORDER`
  纯关键字且 `timeout` 排在 `script` 前 → rc=1 脚本崩溃只要正文出现 `subprocess_no_timeout` 之类的**检查项名**
  或引文里的 "timeout"，就被记成 `timeout`（真值：`cron_incidents` 中 header 为 rc=1 却标 `timeout` 的行 7 条，
  如 `56874d_fab88b245d9f` / `7f7e0d_ca4eaa2c39b0` / `ff334c_a94bf3415e29` ← 该行第 7 行是
  `subprocess_no_timeout: scripts/bk_index_pipeline.py:53`）。分类看的是**未截断的全文**，落库只留前 500 字，
  所以这种误标在台账里肉眼还看不见。
- **本次改动**: ① `_classify_failure_type` 先取权威退出码（`cron.jobs.script_exit_code`，只认前 3 行的
  `Script exited with code N` / `脚本退出码 N`，与 `_is_business_alert` 同源同头形状）→ `N==2` 记 `alert`、
  其它 `N` 记 `script`；**无权威头才走原关键字表**（真超时 `Script timed out after Ns` 无头 → 仍 `timeout`），
  镜像函数不可导入/解析器异常 → 退回关键字路径（fail-safe）；② `_deliver_crash_failure` 里 rc=2 不再写台账，
  投递照旧（该路径收的是异常文本，属防御性覆盖，但「rc=2 不入库」这条不变量必须在**每个**写入点成立）。
- **已验红**: ① 新增测试在 pristine 树上 10/10 全红（4 failures + 6 errors，errors = 新符号不存在）；
  ② 旧分类逻辑复算同一文本得 `timeout`（补丁后 `script`，探针 `~/.hermes/cache/scratch/rc2_red_probe.py`，
  该目录 24h 清理，结论已复写在本节）；
  ③ 写入端把 `_is_business_alert` 打桩为 False（= 补丁前）时 rc=2 文本**真的**进台账 1 条（有门禁 0 条）。
- **已验绿**: 新增 10/10 通过；既有套件 `test_cron_incidents.py` + `test_cron_incidents_resolve.py` +
  `test_cron_business_alert.py` + `test_cron_failure_deliver.py` + `test_jobs.py` = 187 passed；
  `pytest tests/cron` 全目录 1417 passed / 1 failed（`test_mirror_origin_fallback.py::
  test_all_broadcast_does_not_mirror`，该失败在 pristine 树上逐字节复现 —— 本机缺 WEIXIN token 的环境性
  失败，与本项无关，已用临时回退验证）。
- **重放验证**: 复跑 `venv/bin/python3 -m unittest tests.cron.test_cron_incidents_rc2_gate`（红→绿证据）；
  `cron/incidents.py` 侧符号 `_authoritative_exit_code` / `_alert_exit_code`，`cron/scheduler.py` 侧
  `_deliver_crash_failure` 内的 `_is_business_alert(err_text)` 分支。**待 owner 补**: `patch_manifest.py`
  P19 条目的 `files`/`verify` 与 `verify_patches.sh` 注入点（scripts 仓在途改动 + L1 保护面，避免混线；
  本轮未动 scripts/ 任何文件）。待办 853482 追加子项（消费侧 `scripts/cron_exit_codes.py`:
  `classify_error_text` 退出码优先）已于 2026-10-02 落地（commit a28f72f），本轮复核仍为「退出码优先 +
  打断标记仅调度器头部」，无需再改；该文件**不在**保护三清单内（`protect_list_audit.py` 实测）。
- **升级注意**: 上游若把 `failure_type` 改成枚举/退出码驱动，本项与上游合流 → 先读 `_classify_failure_type`
  与 `cron/jobs.py::script_exit_code` 是否已有一份权威解析再决定弃留；升级后 rc=2 若又出现在
  `cron_incidents`（`SELECT COUNT(*) FROM cron_incidents WHERE error LIKE 'Script exited with code 2%'`）
  即门禁失效。
- **回滚**: `git checkout -- cron/incidents.py cron/scheduler.py`；新增测试文件可留（红态即证据）。
  无数据迁移：历史行按原样留档，旧误标行不追改。

#### P19 增补 2026-10-14 — rc=2 分支亦解析存量条目（待办 256052 / 注入点4·5）

- **背景**: P19 写入端闸门只堵住「不再新建 rc=2 条目」，未给存量行退出路径。`cron_incidents` 的两种终结
  都在别处：`closed`（操作员 ack，治理口径刻意排除告警型 —— ack 会静默该签名的失败提醒）、
  `resolved`（只由 `_resolve_incidents_for_recovered_job` 在 **success** 跑次触发）。于是「只出 rc=2、
  从不 success」的作业（`系统·代码健康扫描` / `管线·告警守护` / `报告·格式合规周检` 自 2026-10-01 起
  逐跑次全 rc=2）其存量行**永远留在未确认面**：实测 12 条 / 滞留 8~11 天，且周检门限只看「真失败」
  故永不亮灯（账涨而灯不亮）。
- **本次改动**: `cron/scheduler.py` 的 rc=2 分支在投递之后追加 `_resolve_incidents_for_recovered_job(job)`
  （与 success 分支同一调用）。语义依据: rc=2 = 「脚本跑完并已自报本次发现」，与消费侧
  `cron_governance_weekly._runs_since` 把 rc=2 计入「正常运行」同口径。`resolved ≠ 静默`: 投递抑制
  （`_upsert_incident_for_failure`）只看 `closed` 态与 `alerted_at` 冷却；同签名再犯由 `upsert_incident`
  重新开条（`resolved → detected`）并重置冷却窗口。
- **不变量**: `closed`（操作员 ack）不被改写；解析按 `job_id` 隔离；rc=1 / rc=7 / timeout / 解析不出仍走
  失败路径（fail-safe，不解析）。
- **已验绿**: `tests/cron/test_cron_incidents_rc2_gate.py` = 14 passed / 13 subtests（新增
  `TestRc2AlsoResolvesStaleIncidents` 4 例: rc=2 解析存量行且不新增条目 / ack 不被改写 / rc=1 不解析 /
  只解析本作业）；同族 `test_cron_incidents.py` + `test_cron_incidents_resolve.py` +
  `test_cron_business_alert.py` + `test_cron_failure_deliver.py` = 59 passed。
- **重放验证**: `bash ~/.hermes/scripts/verify_patches.sh` → P19注入点 1/2/3/4/5 全过（4 = 标记
  `P19 注入点4`；5 = `_resolve_incidents_for_recovered_job(job)`）。**上一轮遗留的「待 owner 补
  verify_patches.sh 注入点」到此关闭**；按 L1 保护面流程执行: `audit/protect_list_audit.py` 对账 →
  同进程 chmod 644→写→444 → `script_protect.py --snapshot` 重打 L1 基线（留痕哈希 b02cc55a→78b8b5b8）。
- **生效条件**: 调度器运行在网关进程内 → 需**重启网关**后生效；重启前的跑次不会解析存量行。
- **回滚**: `git checkout -- cron/scheduler.py tests/cron/test_cron_incidents_rc2_gate.py`；
  `verify_patches.sh` 从 `verify_patches.sh.bak-20261014-p19inj45` 还原后重打 L1 基线。无数据迁移:
  已 resolved 的历史行留档，不追改。

### P20. 进程环境注入的密钥纳入值级脱敏注册表 — 2026-10-11 建立

- **文件**: `agent/redact.py`（`register_injected_secret_values` + `_INJECTED_SECRET_MIN_LEN`）、
  `hermes_cli/env_loader.py`（`_register_source_values_for_redaction` + `_apply_external_secret_sources` 调用点）、
  `tests/agent/test_injected_secret_redaction.py`（新，10 用例）、
  `tests/secret_sources/test_injected_secret_registration.py`（新，2 用例）
- **背景**: 凭据暴露面审计（待办 478617）值级扫描确认：会话库 + 磁盘面 18 个「组×键」明文命中，4 次进会话
  全部 `tool_name=terminal`。根因不在形状（正则只认 `sk-`/`AKIA` 是次要因素），而在**命令形状分类覆盖不全**：
  `_command_reads_secret_file` 只认 `.env` / shell rc / `config.yaml` 的读取，而实测泄漏形态是
  ①`ssh host 'sudo sed …'` 远端文件打印 ②`sed -n '60,185p' /tmp/shell-env.txt`（env dump 副本）
  ③python heredoc 打印片段 ④任意首 token 的 echo/--help 输出 —— 四者都不进赋值掩码分支（`code_file=True` 直接跳过）。
- **本次改动**: ① 值级注册表本就是「模型外发边界」——`redact_sensitive_text()` 首行无条件擦已登记值，
  不受 `security.redact_secrets` 开关门控 → 把 **profile 自身注入的生效值**（外部密钥源 supply 的，含被既有
  值胜出的名字）登记进去，得到与命令形状无关的兜底；② 登记带门槛（值长 ≥8 且（名字含凭据词 或 形状不透明）），
  因为擦除是精确子串替换：同源短标识符（`NC_ADMIN_USER=hermes`）会把 "hermes" 在全系统输出里擦掉；
  ③ 注入路径 fail-open，登记异常绝不阻断启动。
- **已验红**: base（只回退实现、留测试）→ 行为断言失败 1（`'fak_…' not in '…'`）+ 缺符号 collection error 1；
  打补丁 → 12/12 通过。邻接回归 17 文件 363 通过 / 7 失败，失败集合与 base 逐条相同（worktree 内跑套件的
  `home_io_guard` 环境性失败，零差集）。生产树（基线含本地补丁）另跑 4 文件 249 通过。
- **重放验证**: `verify_patches.sh` 的 `P20注入点1`（登记接口）/ `P20注入点2`（注入后登记调用点）/
  `P20注入点3`（防误掩码门槛）/ `P20测试`
- **升级注意**: 上游若自行登记注入密钥（等价能力进 main），本项与上游语义合流 → 先读
  `_apply_external_secret_sources` 是否已有登记调用再决定弃留；上游 `redact.py` / `env_loader.py` 大改时按
  `git show` 3way 重放。
- **回滚**: `git checkout -- agent/redact.py hermes_cli/env_loader.py` + 删两个新测试文件即恢复（纯增函数，
  无数据迁移）
- **生效条件**: 改的是**进程启动路径**（env 载入）+ 脱敏函数 → 需重启承载进程（gateway / cron 子进程）才生效；
  未重启前行为不变（新旧并存安全）。**R2 凭据轮换必须在本项生效之后**，否则轮换当天可能再漏一遍。
- **上游化状态**: 分支 `fix/injected-secret-redaction` 已推送 fork（`a3c7197352`，PR 正文 `~/pr_body.md`）；
  受 C3 闸门（在途 PR 8 / 阈值 4 = review_required）阻塞 → 未开 PR，本地 carry。

### P21. 中断跑落账：owner 死亡 → incident + 非 ok last_status — 2026-10-17 建立（待办 379339）

- **文件**: `cron/executions.py`（`_record_owner_loss` + `recover_interrupted_executions` 调用点）、
  `hermes_cli/cron.py`（doctor 的 `unknown` 分支）、
  `tests/cron/test_executions_owner_loss_accounting.py`（新，5 用例）
- **背景**: 网关重启 / kill -9 时，`recover_interrupted_executions()` 把在跑的执行行置 `unknown` 就结束了。
  两个后果**都是静默**：① `cron_incidents` 里没有该 job 的条目 → 运维面没有可 ack 的对象，签名的
  重复抑制链（closed → withheld）对这类损失根本不成立；② jobs.json 保持**上一次跑**的 `last_status`
  （通常 `ok`）直到该 job 下次开火 → `hermes cron list` / doctor 显示绿色，被杀的跑读作「什么都没发生」。
  实证：10-15 实盘·ETF再平衡被上游卡流拖死后，只有 interrupted_detector 的临时告警发现，台账无痕。
- **本次改动**: 恢复路径在 ledger 事务**之外**逐条补两笔写入（`_record_owner_loss`，两笔都 best-effort，
  异常只落 debug，不得中断清扫）：
  ① `cron.incidents.upsert_incident(job_id, reason)` —— 与同步失败路径**同一错误签名**，故 operator
  `ack`（`closed`）后同签名再犯由既有 `_upsert_incident_for_failure` 抑制复报；
  ② `last_status='unknown'` + `last_error` + `last_interrupted_at` + `last_interrupted_execution`
  （执行行 id，证据链）落 jobs.json。
- **为什么是 `unknown` 而不是 `error`**: `cron/jobs.py::_job_is_stale_error_recurring` 只对 `error`/`alert`
  做 stale 重臂 —— 用 `error` 会把一个**仍按计划在跑**的 job 的 `next_run_at` 挪到「现在」，等于人为
  制造提前开火（与 interrupted_detector 的自动补跑叠加即双跑）。`unknown` 不进这条路径。
- **不变量**: ① 不复写「更新的一次已成功跑次」的 `last_status`（`last_run_at > 本次 claimed_at` 且
  `ok`/`completed` → 只留 ledger，不建 incident、不降级状态）；② 不碰 `fire_claim` / `run_claim` /
  `pending_slot` / `next_run_at`（处理的是已死 owner，不能放开并发窗口）；③ 活跃 owner（含本进程）不处理。
- **配套（scripts 侧，同日落）**: `cron_exit_codes.py` 的 `TRIAGE_STATUSES` 增收 `unknown`，且
  `classify_job_state` 对 `unknown` 按产出分流 —— 有终稿 → `interrupted`（可见、不计失败分子），
  无终稿 → `fail`（纪律3 未知按失败）。不改这两处则新状态既不进失败清单也不进中断清单 = 静默消失。
- **已验红绿**: 红（只回退实现、留测试）= 5 用例中 4 失败、1 通过（活跃 owner 用例本就在 base 通过）；
  绿 = 5/5。邻接 9 文件 110 通过 0 失败（execution_ledger / incidents ×3 / persisted-error-recovery /
  stale-running-recovery / warning-execution-outcome / hermes_cli test_cron ×2）。
  预演脚本（`drill_cron_unknown_ledger.py` 指向 worktree 的副本）：A/B/C/D 四项**全绿**，D 详情
  `withheld=True`，修复前基线形态 = A 绿 / B·C·D 红。
- **生效条件**: 恢复逻辑跑在**网关进程内**（provider start / tick）→ 需重启网关后生效。未重启时行为不变
  （旧代码不写这两笔，无迁移）。
- **回滚**: `git checkout -- cron/executions.py hermes_cli/cron.py` + 删新测试文件；脚本侧
  `git checkout -- cron_exit_codes.py tests/test_cron_exit_codes.py`。无数据迁移：已写的 incident 行可
  `hermes cron incidents ack` 收口，已置 `unknown` 的 job 下次开火自然被覆盖。
- **上游化状态**: 判为可上游（缺陷在上游执行器语义内），但 C3 闸门当前 review_required → `PR_INTENT=blocked`，
  本轮只本地 carry。

### P22. browser_exec 守护进程按浏览器会话命名（修 vault 取不到 page origin）— 2026-10-19 建立（待办 301912）

- **文件**: `tools/browser_use_cli.py`（新增 `_daemon_name_for_session` / `_endpoint_reachable`，
  `browser_exec` 内为非具名调用设定 `BU_NAME`）、`tests/tools/test_browser_use_cli.py`（同文件新增 4 例）
- **背景**: `browser_vault_fill` 恒报 `Could not determine the current page origin`、
  `browser_vault_save_login` 报 `Open the site's login page first`（同根因）。进程内探针实测：
  `SUPERVISOR_REGISTRY` 在位、`_eval_js('window.location.href')` → `about:blank`、`focus_page` →
  `no open page on <origin>`，而 CLI 侧 `Target.getTargets` 能看到目标页 ⇒ **双浏览器**：vault supervisor
  绑在内置 agent-browser 那只，browser-use CLI 驱动的是它自己的守护进程浏览器。后果是网页登录（路由器、
  控制台）在桌面端只能人工输密码。
- **本补丁针对的那一层**: harness 守护进程**只按 `BU_NAME` 识别**，`ensure_daemon` 见到同名 daemon 能答
  `Target.getTargets` 就早返回 —— 先起的那个 daemon 长期服务后续所有调用，本次解析出的 `BU_CDP_URL` /
  `BU_CDP_WS` 被静默忽略。非具名 `browser_exec` 过去一律用 `default` ⇒ relay 指向的浏览器与本次真正解析的
  浏览器不是同一个（P22 是单浏览器修法的一侧；CLI 端口径的另一侧见待办 301912 的后续评估）。
- **本次改动**: ① `_daemon_name_for_session(key)` = `hx-<sha1(session key)[:12]>`，一个 Hermes 浏览器会话
  一个 daemon（relay 与浏览器 1:1，陈旧 relay 无法再代别的浏览器应答）；② 端点变更且旧端点仍活
  （`/browser connect`、real-profile 开关、provider 切换）时名字再分叉 `-<sha1(endpoint)[:6]>`；
  ③ 具名 `session` 路径不变（仍 `BU_NAME=session`，harness 自己按名字命名空间）；④ 旧端点不可达时**不**改名，
  交给 `ensure_daemon` 的重启路径用新端点自愈。
- **不变量**: 名字在 `_route_backend` 之后才设（不干扰路由判定）；不碰 `BU_CDP_*` 取值；`_DAEMON_ENDPOINTS`
  是**进程内**状态（重启即清空 → 最坏多分叉一次，不会复用别人的 relay）；不涉密文路径，vault 读取/注入语义不变。
- **已验**: 夹具 `http://127.0.0.1:8817/login.html` 上 `browser_vault_fill` = `success`、`pwd_len=22`（不回显），
  端点变更自愈场景同过；`tests/tools/test_browser_use_cli.py` 112 通过 0 失败（含新增 4 例）。
- **重放验证**: `verify_patches.sh` 的 `P22注入点1`（命名助手）/ `P22注入点2`（`browser_exec` 内赋值）/
  `P22注入点3`（端点分叉 + 可达性探测）/ `P22测试`
- **true-arm 补验（诚实口径）**: `browser.use_real_profile=true` 于 2026-10-19 14:50 实测 —— 临时把
  默认浏览器切到 `microsoft-edge.desktop` 后 `browser_exec` 恢复（此前硬失败于 `tools/browser_tool_real_profile.py:133`，
  与 vault 链路无关），同进程探针 `browser_vault_fill` = `success`、密码框 `value.length=20`（不回显）⇒ **两臂均通**。
  遗留（另立条目，不属 P22）：`browser_vault_*` 不带 `task_id` 时按 `"default"` 找 supervisor，与 `browser_exec`
  注册的会话 task id 不是同一个键 → 真机首次 fill 仍可能报「Could not determine the current page origin」，
  supervisor 重新 attach 后重试成功。
- **上游化状态**: 可上游（daemon 只按名字识别、忽略传入端点属上游行为），但 C3 闸门当前 review_required →
  `PR_INTENT=blocked`，本轮只本地 carry。

### P23. gateway 退出路径 NameError 修复（transcript flush 从未执行）— 2026-10-19 建立（待办 761387）

- **文件**: `tui_gateway/server.py`（新增 `_exit_shutdown_steps()`，`_shutdown_sessions()` 改走它）
- **来源**: v0.22.2 自身的缺陷，不是移植引入 —— 裸 tag 树同样复现，故属**上游缺陷本地先行修**。
- **现象**: 进程退出时 `Exception ignored in atexit callback ... NameError: name
  '_flush_sessions_before_exit' is not defined`（`tui_gateway/server.py:395`）。
- **根因**: 上游把 teardown 三步写成裸名元组
  `(_flush_sessions_before_exit, _release_gateway_wake_owner, _stop_turns_before_exit)`，
  而这三者在 `server.py` 里**从未绑定**：真身分别在 `session_reaper.py:84`、
  `methods_voice.py:338`、`session_reaper.py:120`，模块级导入只给了别名
  `session_reaper as _session_reaper` / `methods_voice as _methods_voice`。
  NameError 发生在**元组构造阶段**，位于 `contextlib.suppress(Exception)` 之外 ⇒ 三步全部跳过：
  transcript 收尾 flush、gateway wake owner 释放、turn 停止都不执行。后果是每次 gateway 退出
  （升级、重启、崩溃恢复）都可能丢掉 flush 预算内的会话记录，且只留一条 "Exception ignored" 噪音。
- **本次改动**: 新增惰性解析器 `_exit_shutdown_steps()`，从 `tui_gateway.session_reaper` /
  `tui_gateway.methods_voice` 取三个真身，导入失败返回 `()`（退出路径永不因导入失败而炸）；
  `_shutdown_sessions()` 改为 `for step in _exit_shutdown_steps()`。
- **不变量**: 不改退出顺序（flush → release → stop），不改 session 关闭逻辑，不改任何 RPC/契约面；
  解析器只在退出路径调用，不影响运行期导入图（懒导入，规避循环依赖）。
- **已验**: 以 gateway 同款启动方式（`tools/python-3.14.7* + -I + sys.path.insert(tree) + import hermes_cli.main`）
  解析出三步全部 callable：
  `['tui_gateway.session_reaper._flush_sessions_before_exit', 'tui_gateway.methods_voice._release_gateway_wake_owner', 'tui_gateway.session_reaper._stop_turns_before_exit']`；
  修复前同一路径直接 NameError。
- **重放验证**: `verify_patches.sh` 的 `P23注入点1`（解析器存在）/ `P23注入点2`（`_shutdown_sessions` 走解析器）/
  `P23契约`（三步名字仍在 server.py 可见，上游改名即红）
- **上游化状态**: 可上游（纯 bug fix，无本地语义），下一窗口随补丁集提交 PR。

### P24. methods_onboarding 缺 `from pathlib import Path`（模块导入即 NameError → tests/cron 整目录收集中断）— 2026-10-19 建立（待办 488947）

- **文件**: `tui_gateway/methods_onboarding.py`（+1 行 import）
- **来源**: **上游缺陷**，且**上游已修**：`95610a5db4`（2026-10-14，「fix(onboarding): import Path in methods_onboarding (serve startup NameError)」）改的就是这一行，但该提交未进入 v0.22.2 发布标签（`git merge-base --is-ancestor 95610a5db4 v0.22.2` = 否；canary.20261017T065512Z 起才含）。裸 tag 与本地候选树同样中招 ⇒ 非移植引入。
- **现象**: `from tui_gateway import server` 即抛
  `NameError: name 'Path' is not defined`（`methods_onboarding.py:71`，`_shared_nous_store()` 返回注解 `tuple[Path, ...]`），模块只 `import threading`。
  后果：任何导入 server 的测试文件收集即炸（`tests/cron/test_cron_manage_profile_scope.py`）→ pytest **整目录被中断**，夜间全量回归/定向裁决只能 `--ignore` 该文件。
  另注：`_run_state` 里 `home = Path(_profile_home("default") ...)` 是运行期用法，故不只是注解问题，onboarding 状态路径本身也会 NameError。
- **本次改动**: 逐字采用上游 `95610a5db4` —— `import threading` 之后加 `from pathlib import Path`。无其它改动。
- **不变量**: 不改 onboarding 语义、返回形状、RPC 面；纯导入补全。
- **已验（2026-10-19 22:2x，venv python 3.12.13 / pytest 9.1.1）**:
  - 修复前 `pytest --collect-only tests/cron/test_cron_manage_profile_scope.py` → `NameError` + `Interrupted: 1 error`；
  - 修复后同命令 `2 tests collected` rc=0，实跑 `2 passed`；
  - `pytest --collect-only tests/cron` → **1545 tests collected, rc=0**（此前整目录中断）；
  - `venv/bin/python -c "import tui_gateway.methods_onboarding"` → ok，`_shared_nous_store()` 返回 `(PosixPath('~/.hermes/shared/nous_auth.json'),)`。
- **重放验证**: `verify_patches.sh` 的 `P24注入点1`（import 行在）/ `P24契约`（Path 仍被该模块使用，上游迁走或改名即红）/ `P24行为`（模块可直接导入，缺则 NameError）。
- **退役条件**: 升级到含 95610a5db4 的 release 后本条 drop（验收：升级后 `git diff <tag> -- tui_gateway/methods_onboarding.py` 对本条为空）。无需开 PR。

### P25. kimi-k3 价格登记（fallback 第 3 跳用量此前落 `cost_status=unknown` / `$0`）— 2026-10-20 建立（待办 340309）

- **文件**: `agent/usage_pricing.py`（+13 行：`_OFFICIAL_DOCS_PRICING` 补 `kimi-k3` 三条别名登记）
- **来源**: 上游价格表缺项（非本站语义）—— `_SNAPSHOTS` 里 Moonshot/Fireworks 家只有 `kimi-k2p6` / `kimi-k2p7-code` / `-fast` 系列，**无 `kimi-k3`**；本机配置里的 provider 名 `kimi-coding-cn` 同样未登记。
- **现象**: 2026-10-20 10:35 家宽上游断 3.2 分钟 → 级联 fallback 到第 3 跳 kimi-k3；当日 `session_model_usage` 中 4 行 kimi-k3（合计 80 次调用、9,945 输入、7,592,424 缓存读、48,776 输出）全部 `cost_status=unknown` / `cost_source=none` / `estimated_cost_usd=0.0` ⇒ **降级成本在账上完全不可见**，按官方 ¥20/¥100/¥2 应计 ≈¥20.3（$2.84）。
- **本次改动**: `del _SNAPSHOTS, ...` 之后新增循环，对 `kimi-coding-cn` / `moonshot` / `moonshot-cn` 三个 provider 别名各登记一条 `kimi-k3`，价格为 USD per M token `2.80 / 14.00 / 0.28`（¥20/¥100/¥2 ÷ 7.15），`version="moonshot-kimi-k3-2026-09"`。
- **不变量**: 只新增价格条目，不改解析顺序、不改 `estimate_usage_cost` 逻辑、不动 `cost_tracker_pricing.PRICING`（脚本侧口径另有一份，两处均已含 kimi-k3）。
- **已验（2026-10-20 11:1x，venv python）**: `estimate_usage_cost("kimi-k3", CanonicalUsage(9945/48776/7592424), provider="kimi-coding-cn", base_url=...)` → `status=estimated`、`source=official_docs_snapshot`、`amount_usd=2.83658872`、`pricing_version=moonshot-kimi-k3-2026-09`；`moonshot-cn` 同值。改前同调用返回 `unknown/0`。
- **重放验证**: `verify_patches.sh` 的 `P25注入点`（登记行在）/ `P25价格版本`（价格快照版本在，兼作回算对账锚点）。
- **退役条件**: 上游 `agent/usage_pricing.py` 自带 `kimi-k3` 后 drop（验收：`git diff <tag> -- agent/usage_pricing.py` 对本条为空）；在此之前可提 PR 上游化（`pr_candidate=True`）。
