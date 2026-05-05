"""
进程控制：跟踪当前活跃的 Claude 运行实例，支持 /stop 和自动打断。
"""

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


@dataclass
class ActiveRun:
    user_id: str
    card_msg_id: str
    proc: object = None
    stop_requested: bool = False
    stop_announced: bool = False


class ActiveRunRegistry:
    def __init__(self):
        self._runs: dict[str, ActiveRun] = {}

    def start(self, user_id: str, card_msg_id: str) -> ActiveRun:
        run = ActiveRun(user_id=user_id, card_msg_id=card_msg_id)
        self._runs[user_id] = run
        return run

    def get(self, user_id: str) -> Optional[ActiveRun]:
        return self._runs.get(user_id)

    def attach_proc(self, user_id: str, proc) -> Optional[ActiveRun]:
        run = self._runs.get(user_id)
        if run is None:
            return None
        run.proc = proc
        if run.stop_requested and getattr(proc, "returncode", None) is None:
            proc.terminate()
        return run

    def clear(self, user_id: str, run: Optional[ActiveRun] = None):
        current = self._runs.get(user_id)
        if current is None:
            return
        if run is not None and current is not run:
            return
        self._runs.pop(user_id, None)


async def stop_run(
    registry: ActiveRunRegistry,
    user_id: str,
    on_stopped: Optional[Callable[[ActiveRun], Awaitable[None] | None]] = None,
    grace: float = 2.0,
) -> bool:
    run = registry.get(user_id)
    if run is None:
        return False

    run.stop_requested = True
    proc = run.proc
    if proc is not None and getattr(proc, "returncode", None) is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    if on_stopped and not run.stop_announced:
        result = on_stopped(run)
        if asyncio.iscoroutine(result):
            await result
        run.stop_announced = True

    return True
