"""本地依赖契约（原先 stub 补在上游测试文件里）— 台账 CUSTOM_PATCHES.md T2。

P7 让 `hermes_cli/gateway.py`（与 `cli.py`）用 `gateway.run.run_with_bounded_executor_shutdown`
包住 `start_gateway()` 协程，以便退出时对 default executor 做有界 join。

上游 `tests/hermes_cli/test_gateway.py` 的两处 fake `gateway.run` 模块只提供
`start_gateway` / `_exit_after_graceful_shutdown` → 本地代码在 fake 下 import 失败、
进程 exit 1，从而**掩盖**了被断言的 daemon 退出码契约（假通过）。
原做法是在上游测试里补 stub；2026-09-25 起上游文件保持原样，改为本文件断言
**符号依赖与注入点存在**——上游一旦改名/删除该 helper，这里立刻红，而不是等到
升级重放时才发现 P7 注入点失效。
"""

from __future__ import annotations

import inspect
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class TestBoundedExecutorShutdownContract:
    def test_symbol_exists_in_gateway_run(self):
        from gateway import run as gateway_run

        helper = getattr(gateway_run, "run_with_bounded_executor_shutdown", None)
        assert callable(helper), (
            "上游 library/gateway/run.py 不再提供 run_with_bounded_executor_shutdown "
            "→ P7（退出卡 300s）注入点失效，需重做本地补丁（CUSTOM_PATCHES.md P7）"
        )
        assert inspect.iscoroutinefunction(helper) or callable(helper)

    def test_local_call_sites_import_from_gateway_run(self):
        """两处本地调用点必须以 `from gateway.run import ...` 形式取得该 helper。"""
        for rel in ("hermes_cli/gateway.py", "cli.py"):
            src = (REPO / rel).read_text(encoding="utf-8")
            assert "run_with_bounded_executor_shutdown" in src, f"{rel} 缺 P7 注入点"
            assert "from gateway.run import" in src, f"{rel} 缺 gateway.run 的 import"
            import_lines = [
                ln for ln in src.splitlines()
                if ln.strip().startswith("from gateway.run import")
            ]
            assert any("run_with_bounded_executor_shutdown" in ln for ln in import_lines), (
                f"{rel}: 该 helper 必须与 start_gateway 同批从 gateway.run 导入"
                "（P7 的有界 join 依赖此绑定）"
            )
