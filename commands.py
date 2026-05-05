"""
斜杠命令解析与处理。返回回复文本（str）或 dict(text, buttons)，None 表示转发给 Claude。
"""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from typing import Optional

from config import CLAUDE_CLI, DEFAULT_CWD, claude_cmd
from session import SessionStore, scan_cli_sessions

VALID_MODES = {
    "default":            "每次工具调用需确认",
    "acceptEdits":        "自动接受文件编辑，其余需确认",
    "plan":               "只规划不执行工具",
    "bypassPermissions":  "全部自动执行（无确认）",
}

MODE_ALIASES = {
    "bypass": "bypassPermissions",
    "accept": "acceptEdits",
    "auto":   "bypassPermissions",
}

MODEL_ALIASES = {
    "opus":   "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5-20251001",
}

HELP_TEXT = """\
📖 **可用命令**

**会话管理：**
`/new` 或 `/clear` — 开始新 session
`/resume` — 查看 / 恢复历史 session
`/stop` — 停止当前任务
`/status` — 当前 session 信息

**配置：**
`/model [名称]` — 切换模型（opus / sonnet / haiku）
`/mode [模式]` — 切换权限模式
`/cd [路径]` — 切换工作目录
`/ws` — 工作空间管理

**查看：**
`/ls [路径]` — 列出目录内容
`/skills` — 已安装 Skills
`/mcp` — 已配置 MCP Servers
`/usage` — Claude Max 用量
`/help` — 显示此帮助

**其他 `/xxx` 命令** 自动转发给 Claude 执行（如 /commit）。\
"""

BOT_COMMANDS = {
    "help", "h", "new", "clear", "resume", "model", "mode", "status",
    "cd", "ls", "workspace", "ws", "skills", "mcp", "usage", "stop",
}


def parse_command(text: str) -> Optional[tuple[str, str]]:
    text = text.strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split(None, 1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return cmd, args


# ── 辅助函数 ──────────────────────────────────────────────────

def _strip_md(text: str) -> str:
    text = " ".join(text.split())
    while text.startswith("#"):
        text = text.lstrip("#").lstrip()
    return text.replace("**", "").replace("__", "").replace("`", "").strip()


def _fmt_time(raw: str) -> str:
    t = raw[:16].replace("T", " ")
    return t[5:16].replace("-", "/") if len(t) >= 16 else t


async def _merged_sessions(user_id: str, chat_id: str, store: SessionStore,
                            cli_all: Optional[list] = None) -> list[dict]:
    """合并飞书历史和 CLI 历史，去重排序，不含当前 session。"""
    cur_sid = (await store.get_current_raw(user_id, chat_id)).get("session_id")
    if cli_all is None:
        cli_all = scan_cli_sessions(30)
    cli_map = {s["session_id"]: s for s in cli_all}

    feishu_sessions = [{**s, "source": "feishu"}
                       for s in await store.list_sessions(user_id, chat_id)]
    for s in feishu_sessions:
        if s["session_id"] in cli_map and cli_map[s["session_id"]].get("preview"):
            s["preview"] = cli_map[s["session_id"]]["preview"]

    feishu_ids = {s["session_id"] for s in feishu_sessions}
    cli_only = [s for s in cli_all
                if s["session_id"] not in feishu_ids and len(s.get("preview", "")) > 5]

    seen = {cur_sid} if cur_sid else set()
    merged = []
    for s in feishu_sessions + cli_only:
        if s["session_id"] not in seen:
            seen.add(s["session_id"])
            merged.append(s)

    merged.sort(key=lambda s: s.get("started_at", ""), reverse=True)
    return merged[:15]


async def _session_list_reply(user_id: str, chat_id: str, store: SessionStore):
    """构造 /resume 回复（带按钮）。"""
    from session import _clean_text

    cur = await store.get_current_raw(user_id, chat_id)
    cur_sid = cur.get("session_id")
    cli_all = scan_cli_sessions(30)
    cli_map = {s["session_id"]: s for s in cli_all}
    all_sessions = await _merged_sessions(user_id, chat_id, store, cli_all)

    if not cur_sid and not all_sessions:
        return "暂无历史 sessions。"

    summaries = {}
    missing = []
    for sid in ([cur_sid] if cur_sid else []) + [s["session_id"] for s in all_sessions]:
        cached = store.get_summary(user_id, sid)
        if cached:
            summaries[sid] = cached
        else:
            missing.append(sid)
    for sid in missing[:5]:
        asyncio.create_task(store._bg_summarize(user_id, sid))

    def _desc(sid: str, preview: str) -> str:
        s = _strip_md(summaries.get(sid, "")) or _strip_md(_clean_text(preview or "")) or "（无预览）"
        return s[:30] if len(s) <= 30 else s[:28] + ".."

    lines = []
    if cur_sid:
        cli_info = cli_map.get(cur_sid)
        preview = (cli_info.get("preview") if cli_info else None) or cur.get("preview") or ""
        lines.append(f"当前：{_desc(cur_sid, preview)} ({_fmt_time(cur.get('started_at', ''))})")
    lines.append(f"共 {len(all_sessions)} 个历史会话")

    buttons = [
        {
            "text": f"{_desc(s['session_id'], s.get('preview', ''))} ({_fmt_time(s.get('started_at', ''))})",
            "value": {"action": "resume_session", "sid": s["session_id"], "cid": chat_id},
        }
        for s in all_sessions[:10]
    ]
    if buttons:
        return {"text": "\n".join(lines), "buttons": buttons}
    return "\n".join(lines)


def _list_skills(chat_id: str = "") -> dict | str:
    skills = []
    for base in (os.path.expanduser("~/.claude/plugins"), os.path.expanduser("~/.claude/skills")):
        if not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            for fname in files:
                if not fname.endswith(".md"):
                    continue
                if os.path.basename(root) not in ("commands", os.path.basename(base)):
                    continue
                name = fname[:-3]
                try:
                    desc = ""
                    with open(os.path.join(root, fname), encoding="utf-8") as f:
                        in_fm = False
                        for line in f:
                            line = line.strip()
                            if line == "---":
                                in_fm = not in_fm
                                if not in_fm:
                                    break
                            elif in_fm and line.startswith("description:"):
                                desc = line[len("description:"):].strip().strip('"')
                except OSError:
                    pass
                skills.append((name, desc))

    if not skills:
        return "暂无已安装的 skills。"

    seen, unique = set(), []
    for name, desc in sorted(skills):
        if name not in seen:
            seen.add(name)
            unique.append((name, desc))

    return {
        "text": f"🛠 **可用 Skills** ({len(unique)} 个)",
        "buttons": [
            {"text": f"/{name}",
             "value": {"action": "reply", "reply": f"/{name}", "cid": chat_id}}
            for name, _ in unique[:15]
        ],
    }


def _list_mcp() -> str:
    try:
        result = subprocess.run(
            claude_cmd("mcp", "list"),
            capture_output=True, text=True, timeout=10,
        )
        output = (result.stdout or "").strip()
        if result.returncode != 0:
            err = (result.stderr or "").strip()
            return f"❌ 获取 MCP 列表失败（exit {result.returncode}）：{err or '无错误信息'}"
    except Exception as e:
        return f"❌ 获取 MCP 列表失败：{e}"
    if not output:
        return "暂无已配置的 MCP servers。"
    return f"🔌 **已配置的 MCP Servers**\n\n{output}"


def _get_usage() -> str:
    """获取 Claude Max 订阅用量（通过 oauth token 发轻量请求）。"""
    import ssl
    import urllib.request
    import urllib.error

    token = None

    # macOS keychain
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            token = json.loads(result.stdout.strip())["claudeAiOauth"]["accessToken"]
        except Exception:
            pass

    # 凭证文件（Windows/Linux 也适用）
    if not token:
        try:
            creds_path = os.path.expanduser("~/.claude/.credentials.json")
            with open(creds_path) as f:
                token = json.load(f)["claudeAiOauth"]["accessToken"]
        except Exception:
            pass

    if not token:
        return "❌ 未找到 Claude oauth 凭证，请先登录 claude CLI。"

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            headers = dict(resp.headers)
    except urllib.error.HTTPError as e:
        headers = dict(e.headers)
    except Exception as e:
        return f"❌ 获取用量失败：{e}"

    def h(key):
        return headers.get(key) or headers.get(key.lower())

    def fmt_pct(val):
        if val is None:
            return "未知"
        pct = float(val) * 100
        filled = round(pct / 100 * 20)
        return f"{'█' * filled}{'░' * (20 - filled)} {pct:.1f}%"

    def fmt_reset(ts):
        if ts is None:
            return "未知"
        try:
            dt = datetime.fromtimestamp(int(ts))
            diff = dt - datetime.now()
            h_left = int(diff.total_seconds() // 3600)
            m_left = int((diff.total_seconds() % 3600) // 60)
            return f"{dt.strftime('%m/%d %H:%M')}（{h_left}h{m_left}m 后）"
        except Exception:
            return ts

    u5h = h("anthropic-ratelimit-unified-5h-utilization")
    u7d = h("anthropic-ratelimit-unified-7d-utilization")
    r5h = h("anthropic-ratelimit-unified-5h-reset")
    r7d = h("anthropic-ratelimit-unified-7d-reset")
    s5h = h("anthropic-ratelimit-unified-5h-status") or "unknown"
    s7d = h("anthropic-ratelimit-unified-7d-status") or "unknown"

    if u5h is None and u7d is None:
        return "📊 **Usage**\n\n未能获取用量数据。"

    return (
        f"📊 **Claude Max 用量**\n\n"
        f"**5小时窗口**（状态：{s5h}）\n{fmt_pct(u5h)}\n重置：{fmt_reset(r5h)}\n\n"
        f"**7天窗口**（状态：{s7d}）\n{fmt_pct(u7d)}\n重置：{fmt_reset(r7d)}"
    )


async def _handle_workspace(args: str, user_id: str, chat_id: str,
                             store: SessionStore) -> str | dict:
    if not args:
        return await _workspace_list(user_id, chat_id, store)
    try:
        parts = shlex.split(args)
    except ValueError as e:
        return f"❌ 参数解析失败：{e}"
    if not parts:
        return await _workspace_list(user_id, chat_id, store)

    action = parts[0].lower()
    if action in ("list", "ls"):
        return await _workspace_list(user_id, chat_id, store)

    if action in ("save", "add"):
        if len(parts) < 2:
            return "⚠️ 用法：`/ws save 名称 [路径]`"
        name = parts[1]
        path = os.path.expanduser(parts[2]) if len(parts) >= 3 else (
            await store.get_current_raw(user_id, chat_id)).get("cwd", DEFAULT_CWD)
        if not os.path.isdir(path):
            return f"❌ 路径不存在：`{path}`"
        await store.save_workspace(user_id, name, path)
        return f"✅ 已保存工作空间 `{name}` → `{path}`"

    if action == "use":
        if len(parts) != 2:
            return "⚠️ 用法：`/ws use 名称`"
        name = parts[1]
        path = await store.bind_workspace(user_id, chat_id, name)
        if not path:
            return f"❌ 未找到工作空间：`{name}`"
        return f"✅ 已绑定工作空间 `{name}`\n工作目录：`{path}`\n发送 `/new` 可清空旧上下文。"

    if action == "set":
        if len(parts) != 2:
            return "⚠️ 用法：`/ws set 路径`"
        path = os.path.expanduser(parts[1])
        if not os.path.isdir(path):
            return f"❌ 路径不存在：`{path}`"
        await store.set_cwd(user_id, chat_id, path)
        return f"✅ 工作目录已设为 `{path}`"

    if action in ("remove", "delete", "rm"):
        if len(parts) != 2:
            return "⚠️ 用法：`/ws remove 名称`"
        if not await store.delete_workspace(user_id, parts[1]):
            return f"❌ 未找到工作空间：`{parts[1]}`"
        return f"✅ 已删除工作空间 `{parts[1]}`"

    return f"❌ 未知子命令：`{action}`\n可用：`list`、`save`、`use`、`set`、`remove`"


async def _workspace_list(user_id: str, chat_id: str, store: SessionStore) -> str | dict:
    cur = await store.get_current_raw(user_id, chat_id)
    current_name = cur.get("workspace", "")
    current_cwd = cur.get("cwd", "~")
    workspaces = store.list_workspaces(user_id)

    lines = ["🗂 **工作空间**", f"当前：`{current_name or '（未命名）'}` → `{current_cwd}`"]
    buttons = [
        {
            "text": f"📁 {name}{'✓' if name == current_name else ''}",
            "value": {"action": "run_cmd", "cmd": f"/ws use {name}", "cid": chat_id},
        }
        for name in workspaces
    ]
    if buttons:
        lines.append(f"已保存 {len(workspaces)} 个，点击切换：")
        return {"text": "\n".join(lines), "buttons": buttons}
    lines.append("还没有工作空间。用 `/ws save 名称 [路径]` 保存。")
    return "\n".join(lines)


# ── 主入口 ─────────────────────────────────────────────────────

async def handle_command(
    cmd: str, args: str,
    user_id: str, chat_id: str,
    store: SessionStore,
) -> Optional[str | dict]:
    """处理命令。返回 None 表示不是 bot 命令，应转发给 Claude。"""
    if cmd == "ws":
        cmd = "workspace"
    if cmd not in BOT_COMMANDS:
        return None

    if cmd in ("help", "h"):
        return HELP_TEXT

    if cmd in ("new", "clear"):
        new_mode = None
        if args:
            alias = MODE_ALIASES.get(args.lower(), args)
            if alias in VALID_MODES:
                new_mode = alias
        old_title = await store.new_session(user_id, chat_id)
        if new_mode:
            await store.set_permission_mode(user_id, chat_id, new_mode)
        cur = await store.get_current(user_id, chat_id)
        text = f"✅ 已开始新 session。"
        if old_title:
            text += f"\n上个会话：「{old_title}」"
        text += f"\n当前模式：**{cur.permission_mode}**"
        return {
            "text": text,
            "buttons": [
                {"text": "📋 规划",    "value": {"action": "set_mode", "mode": "plan",               "cid": chat_id}},
                {"text": "✏️ 接受编辑", "value": {"action": "set_mode", "mode": "acceptEdits",        "cid": chat_id}},
                {"text": "🚀 全自动",   "value": {"action": "set_mode", "mode": "bypassPermissions",  "cid": chat_id}},
                {"text": "🔒 需确认",   "value": {"action": "set_mode", "mode": "default",            "cid": chat_id}},
            ],
        }

    if cmd == "resume":
        if not args:
            return await _session_list_reply(user_id, chat_id, store)
        try:
            idx = int(args) - 1
            all_sessions = await _merged_sessions(user_id, chat_id, store)
            if 0 <= idx < len(all_sessions):
                args = all_sessions[idx]["session_id"]
            else:
                return f"❌ 序号 {int(args)} 超出范围（共 {len(all_sessions)} 条）。"
        except ValueError:
            pass
        sid, old_title = await store.resume_session(user_id, chat_id, args)
        if not sid:
            return f"❌ 未找到 session：`{args}`"
        name = store.get_summary(user_id, sid) or f"#{sid[:8]}"
        reply = f"✅ 已恢复会话「{name}」，继续对话吧。"
        if old_title:
            reply += f"\n上个会话：「{old_title}」"
        return reply

    if cmd == "model":
        if not args:
            cur = await store.get_current(user_id, chat_id)
            return {
                "text": f"当前模型：**{cur.model}**",
                "buttons": [
                    {"text": "🧠 Opus",   "value": {"action": "run_cmd", "cmd": "/model opus",   "cid": chat_id}},
                    {"text": "⚡ Sonnet", "value": {"action": "run_cmd", "cmd": "/model sonnet", "cid": chat_id}},
                    {"text": "🐇 Haiku",  "value": {"action": "run_cmd", "cmd": "/model haiku",  "cid": chat_id}},
                ],
            }
        model = MODEL_ALIASES.get(args.lower(), args)
        await store.set_model(user_id, chat_id, model)
        return f"✅ 已切换模型为 `{model}`"

    if cmd == "mode":
        if not args:
            cur = await store.get_current(user_id, chat_id)
            return {
                "text": f"当前模式：**{cur.permission_mode}**\n{VALID_MODES.get(cur.permission_mode, '')}",
                "buttons": [
                    {"text": "📋 规划",    "value": {"action": "set_mode", "mode": "plan",              "cid": chat_id}},
                    {"text": "✏️ 接受编辑", "value": {"action": "set_mode", "mode": "acceptEdits",       "cid": chat_id}},
                    {"text": "🚀 全自动",   "value": {"action": "set_mode", "mode": "bypassPermissions", "cid": chat_id}},
                    {"text": "🔒 需确认",   "value": {"action": "set_mode", "mode": "default",           "cid": chat_id}},
                ],
            }
        mode = MODE_ALIASES.get(args.lower(), args)
        if mode not in VALID_MODES:
            return f"❌ 未知模式：`{args}`\n可选：{', '.join(f'`{m}`' for m in VALID_MODES)}"
        await store.set_permission_mode(user_id, chat_id, mode)
        return f"✅ 已切换为 **{mode}** — {VALID_MODES[mode]}"

    if cmd == "status":
        cur = await store.get_current_raw(user_id, chat_id)
        return (
            f"📊 **当前 Session 状态**\n"
            f"Session ID: `{cur.get('session_id') or '（新 session）'}`\n"
            f"模型: `{cur.get('model', '未知')}`\n"
            f"权限模式: `{cur.get('permission_mode', '未知')}`\n"
            f"工作空间: `{cur.get('workspace') or '（未绑定）'}`\n"
            f"工作目录: `{cur.get('cwd', '~')}`\n"
            f"开始时间: {cur.get('started_at', '')[:16].replace('T', ' ')}"
        )

    if cmd == "cd":
        if not args:
            return "⚠️ 用法：`/cd 路径`"
        path = os.path.expanduser(args)
        if not os.path.isdir(path):
            return f"❌ 路径不存在：`{path}`"
        old_ws = (await store.get_current_raw(user_id, chat_id)).get("workspace", "")
        await store.set_cwd(user_id, chat_id, path)
        suffix = "，并解除原工作空间绑定" if old_ws else ""
        return f"✅ 工作目录已切换为 `{path}`{suffix}"

    if cmd == "ls":
        cur = await store.get_current_raw(user_id, chat_id)
        base = cur.get("cwd", DEFAULT_CWD)
        raw = args.strip()
        if not raw:
            target, display = base, "."
        elif os.path.isabs(raw):
            target, display = os.path.expanduser(raw), raw
        else:
            target = os.path.abspath(os.path.join(base, os.path.expanduser(raw)))
            display = raw
        if not os.path.exists(target):
            return f"❌ 路径不存在：`{display}`\n当前工作目录：`{base}`"
        if not os.path.isdir(target):
            return f"❌ 目标不是目录：`{display}`"
        try:
            entries = sorted(
                (not e.is_dir(), e.name.lower(), f"`{e.name}{'/' if e.is_dir() else ''}`")
                for e in os.scandir(target)
            )
        except OSError as e:
            return f"❌ 读取目录失败：{e}"
        preview = [item[2] for item in entries[:50]]
        hidden = max(0, len(entries) - len(preview))
        lines = ["📁 **目录内容**", f"路径：`{target}`", ""]
        lines.extend(preview)
        if hidden:
            lines.append(f"\n…… 还有 {hidden} 项")
        return "\n".join(lines)

    if cmd == "workspace":
        return await _handle_workspace(args, user_id, chat_id, store)

    if cmd == "skills":
        return _list_skills(chat_id)

    if cmd == "mcp":
        return _list_mcp()

    if cmd == "usage":
        return _get_usage()

    if cmd == "stop":
        return "⏹ /stop 在消息队列外处理，当前无运行中的任务。"

    return None
