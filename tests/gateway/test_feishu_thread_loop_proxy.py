"""Regression tests for the Lark WS module-level loop thread-local proxy.

背景: lark_oapi 的 ws/client.py 用模块级全局 `loop` (larksuite/oapi-sdk-python
Issue #119, 官方 1.6.8/1.7.3 未修)。双实例 (default/game-dev) 各自线程在
`_run_official_feishu_ws_client` 里 `ws_client_module.loop = <自己的 loop>`
互相覆盖 → 断连重连时 SDK 内部 `loop.create_task()` 拿到另一实例的 loop →
RuntimeError "attached to a different loop" → 接收循环死、SDK 不重连 → 入站黑洞
(8/19、8/20、8/25 三次实证, 每次 2h 级)。

修复: 模块级 `loop` 只安装一次「线程本地代理」, 每线程访问时解析到本线程的 loop。

⚠️ 2026-08-24 `hermes update` 的 `git reset --hard origin/main` 清除了补丁与其
配套单测 → 08-25 05:00:39:906 跨 loop RuntimeError 再现。本文件为 08-25 重放
补丁后补建的防回归防线 (原 4 单测已随 reset 丢失)。
"""

import asyncio
import threading

import pytest

import lark_oapi.ws.client as ws_client_module
from plugins.platforms.feishu import adapter as feishu_adapter
from plugins.platforms.feishu.adapter import _ThreadLocalLoopProxy, _run_official_feishu_ws_client


async def _noop() -> None:
    """立即完成的任务 —— 用于在目标 loop 上创建 task 并捕获其归属。"""


class _FakeAdapter:
    """最小 adapter 替身: 只暴露 _run_official_feishu_ws_client 用到的属性。"""

    def __init__(self) -> None:
        self._ws_thread_loop: asyncio.AbstractEventLoop | None = None
        self._ws_reconnect_nonce = 0
        self._ws_reconnect_interval = 1.0
        self._ws_ping_interval = 30


class _FakeWSClient:
    """最小 ws_client 替身: start() 模拟 SDK 在该线程阻塞运行, 记录关键证据。"""

    def __init__(self, recorder: list) -> None:
        self._recorder = recorder

    def start(self) -> None:
        # 模拟 SDK 内部对模块级 loop 的方法调用: create_task 必须解析到
        # 当前线程自己的 loop (proxy 的 __getattr__ 转发)。
        task = ws_client_module.loop.create_task(_noop())
        self._recorder.append(("task_loop", task.get_loop()))
        self._recorder.append(("proxy", ws_client_module.loop))
        # v0.21.1 起上游把「安装一次」的判据从 module 属性
        # `_hermes_thread_loop_proxy_installed` 换成 adapter 侧的
        # `_WS_ISOLATION_INSTALLED` + `_ThreadLocalLoopProxy`。这里改测
        # 行为等价物: 模块级 loop 已是线程本地代理。
        self._recorder.append(("installed", isinstance(ws_client_module.loop, _ThreadLocalLoopProxy)))


def _run_ws_client_in_thread(recorder: list) -> None:
    """在独立线程里跑一次完整的 _run_official_feishu_ws_client。"""
    adapter = _FakeAdapter()
    ws = _FakeWSClient(recorder)
    _run_official_feishu_ws_client(ws, adapter)
    # finally 分支: 线程 loop 应被清理
    recorder.append(("ws_thread_loop_cleared", adapter._ws_thread_loop is None))


# 进程原始全局 (导入期快照): 详见 _restore_sdk_globals 的清理语义。
_PRISTINE_WS_LOOP = getattr(ws_client_module, "loop", None)
_PRISTINE_WS_CONNECT = getattr(ws_client_module.websockets, "connect", None)


@pytest.fixture(autouse=True)
def _restore_sdk_globals():
    """把 SDK 模块级全局复位到进程原始状态。

    安装是幂等的进程级副作用 (v0.21.1: adapter 侧 `_WS_ISOLATION_INSTALLED`),
    同批次跑其他飞书用例 (如 test_feishu_sdk_executor) 会把它置位; 若不
    复位到「未安装」, 本文件的 `_install_lark_ws_isolation` 会短路返回,
    模块级 loop 保持裸 loop → 线程隔离断言假失败 (实测: 单跑绿, 合跑红)。
    故 setup/teardown 双侧复位, 用例与执行顺序无关。
    """
    _reset_sdk_globals()
    yield
    _reset_sdk_globals()


def _reset_sdk_globals() -> None:
    ws_client_module.loop = _PRISTINE_WS_LOOP
    ws_client_module.websockets.connect = _PRISTINE_WS_CONNECT
    feishu_adapter._WS_ISOLATION_INSTALLED = False


def _run_thread(target) -> None:
    th = threading.Thread(target=target)
    th.start()
    th.join()
    assert not th.is_alive()


def test_single_thread_installs_proxy_and_resolves_own_loop():
    """单实例: 安装一次 proxy, 线程内 create_task 解析到本线程 loop。"""
    rec: list = []
    _run_thread(lambda: _run_ws_client_in_thread(rec))

    task_loops = [v for k, v in rec if k == "task_loop"]
    assert len(task_loops) == 1
    # proxy 转发必须解析到真实 event loop (不是 proxy 自身)
    assert isinstance(task_loops[0], asyncio.AbstractEventLoop)
    # 安装标记已置位 (v0.21.1 语义: 模块级 loop 已被代理包装)
    assert isinstance(ws_client_module.loop, _ThreadLocalLoopProxy)
    # 模块级 loop 不再被裸覆盖为某个具体 loop (否则双实例会互相踩)
    assert not isinstance(ws_client_module.loop, asyncio.AbstractEventLoop)
    # finally 清理
    assert rec[-1] == ("ws_thread_loop_cleared", True)


def test_two_threads_resolve_distinct_loops():
    """双实例隔离: default/game-dev 各自线程必须解析到各自 loop — 事故核心复现。

    旧代码 (裸赋值) 下, 后启动线程覆盖模块级 loop, 先启动线程的 create_task
    会拿到另一线程的 loop → 本测试断言 task loop 互不相同, 即隔离成立。
    """
    rec: list = []
    threads = [threading.Thread(target=_run_ws_client_in_thread, args=(rec,)) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    task_loops = [v for k, v in rec if k == "task_loop"]
    assert len(task_loops) == 2
    # 两个线程各自拿到自己的 loop (旧 bug 下会相同/交叉)
    assert task_loops[0] is not task_loops[1]


def test_proxy_installed_only_once():
    """防重复安装: 多次调用 (多实例/重启) 后模块级 loop 仍是同一 proxy 对象。"""
    rec: list = []
    for _ in range(2):
        _run_thread(lambda: _run_ws_client_in_thread(rec))

    proxies = [v for k, v in rec if k == "proxy"]
    assert len(proxies) == 2
    assert proxies[0] is proxies[1]  # 同一 proxy 实例, 未被二次安装覆盖
    assert all(v for k, v in rec if k == "installed")


def test_proxy_lazily_creates_loop_in_unbound_thread():
    """惰性创建: 未绑定 loop 的线程访问 proxy 属性时自动 new_event_loop。

    保证 SDK 在任意线程 (无显式 set_event_loop) 调用模块级 loop 也不崩。
    """
    rec: list = []
    _run_thread(lambda: _run_ws_client_in_thread(rec))  # 先完成安装

    got: dict = {}

    def _probe() -> None:
        task = ws_client_module.loop.create_task(_noop())  # 触发 __getattr__
        got["loop"] = task.get_loop()

    _run_thread(_probe)
    assert isinstance(got["loop"], asyncio.AbstractEventLoop)
