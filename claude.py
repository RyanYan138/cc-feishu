"""
通过 subprocess 调用本机 claude CLI，解析 stream-json 输出，流式回调。
"""

import asyncio
import json
import os
import sys
from typing import Callable, Optional

from config import PERMISSION_MODE, CLAUDE_CLI, claude_cmd

IDLE_TIMEOUT = 300    # 5 分钟无输出视为挂死
_CHECK_INTERVAL = 30  # 每 30 秒检查一次


def _has_child_processes(pid: int) -> bool:
    """检测进程是否有子进程（跨平台）。有子进程说明在跑编译/下载等，不应计为空闲。"""
    try:
        if sys.platform == "win32":
            import subprocess
            result = subprocess.run(
                ["wmic", "process", "where", f"ParentProcessId={pid}", "get", "ProcessId"],
                capture_output=True, timeout=5,
            )
            lines = [l.strip() for l in result.stdout.decode(errors="replace").splitlines()
                     if l.strip() and l.strip().isdigit()]
            return len(lines) > 0
        else:
            import subprocess
            result = subprocess.run(
                ["pgrep", "-P", str(pid)], capture_output=True, timeout=5,
            )
            return result.returncode == 0
    except Exception:
        return False


def _extract_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(item.get("text", "") for item in value
                       if isinstance(item, dict) and item.get("type") == "text")
    return ""


async def _fire(cb, *args):
    if cb is None:
        return
    result = cb(*args)
    if asyncio.iscoroutine(result):
        await result


async def run_claude(
    message: str,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    permission_mode: Optional[str] = None,
    on_text_chunk: Optional[Callable[[str], None]] = None,
    on_tool_use: Optional[Callable[[str, dict], None]] = None,
    on_process_start: Optional[Callable] = None,
) -> tuple[str, Optional[str], bool]:
    """
    调用 claude CLI 并流式解析输出。
    返回 (full_text, new_session_id, used_fallback_session)。
    """

    async def _run_once(active_sid: Optional[str]) -> tuple[str, Optional[str], int, str]:
        cmd = claude_cmd(
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode", permission_mode or PERMISSION_MODE,
        )
        if active_sid:
            cmd += ["--resume", active_sid]
        if model:
            cmd += ["--model", model]

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or os.path.expanduser("~"),
            env=env,
            limit=10 * 1024 * 1024,
        )

        await _fire(on_process_start, proc)

        proc.stdin.write((message + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()

        full_text = ""
        new_sid = None
        tool_name = ""
        tool_json = ""
        idle_secs = 0

        try:
            while True:
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=_CHECK_INTERVAL)
                    idle_secs = 0
                except asyncio.TimeoutError:
                    if _has_child_processes(proc.pid):
                        idle_secs = 0
                        continue
                    idle_secs += _CHECK_INTERVAL
                    if idle_secs >= IDLE_TIMEOUT:
                        proc.kill()
                        await proc.wait()
                        raise RuntimeError(
                            f"Claude 超时 ({IDLE_TIMEOUT}s 无输出)，已终止进程"
                        )
                    continue

                if not raw:
                    break

                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = data.get("type")

                if etype == "system":
                    sid = data.get("session_id")
                    if sid:
                        new_sid = sid

                elif etype == "stream_event":
                    evt = data.get("event", {})
                    etype2 = evt.get("type")

                    if etype2 == "content_block_start":
                        block = evt.get("content_block", {})
                        if block.get("type") == "tool_use":
                            tool_name = block.get("name", "")
                            tool_json = ""
                            await _fire(on_tool_use, tool_name, {})

                    elif etype2 == "content_block_delta":
                        delta = evt.get("delta", {})
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            chunk = delta.get("text", "")
                            if chunk:
                                full_text += chunk
                                await _fire(on_text_chunk, chunk)
                        elif dtype == "input_json_delta":
                            tool_json += delta.get("partial_json", "")

                    elif etype2 == "content_block_stop":
                        if tool_name and tool_json:
                            try:
                                inp = json.loads(tool_json)
                            except json.JSONDecodeError:
                                inp = {}
                            await _fire(on_tool_use, tool_name, inp)
                        tool_name = ""
                        tool_json = ""

                elif etype == "result":
                    sid = data.get("session_id")
                    if sid:
                        new_sid = sid
                    final = _extract_text(data.get("result", ""))
                    if final:
                        full_text = final

        except RuntimeError:
            raise

        stderr_bytes = await proc.stderr.read()
        await proc.wait()
        return (
            full_text.strip(),
            new_sid,
            proc.returncode,
            stderr_bytes.decode("utf-8", errors="replace").strip(),
        )

    text, sid, rc, stderr = await _run_once(session_id)
    fallback = False

    # session 与 cwd 不兼容时 CLI 可能静默退出，自动切新 session
    if session_id and rc != 0 and not stderr and not text:
        print("[claude] resume 失败，自动切新 session 重试", flush=True)
        text, sid, rc, stderr = await _run_once(None)
        fallback = True

    if rc != 0:
        detail = stderr or "no stderr"
        if text:
            return text, sid, fallback
        raise RuntimeError(f"claude exited {rc}: {detail}")

    return text, sid, fallback
