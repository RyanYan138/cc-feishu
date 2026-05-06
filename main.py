"""
飞书 × Claude Code Bot
通过飞书 WebSocket 长连接接收私聊/群聊消息，调用本机 claude CLI 回复，流式卡片输出。

启动：python main.py
"""

import asyncio
import json
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import lark_oapi as lark
from lark_oapi.api.im.v1.model import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackToast,
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

import config
from feishu import FeishuClient
from session import SessionStore
from commands import VALID_MODES, handle_command, parse_command
from claude import run_claude
from run_control import ActiveRunRegistry, stop_run

# ── 全局事件循环（独立线程驱动）───────────────────────────────

_bot_loop = asyncio.new_event_loop()


def _run_loop():
    asyncio.set_event_loop(_bot_loop)
    _bot_loop.run_forever()


threading.Thread(target=_run_loop, daemon=True, name="bot-loop").start()

# ── 全局单例 ──────────────────────────────────────────────────

_lark = lark.Client.builder() \
    .app_id(config.FEISHU_APP_ID) \
    .app_secret(config.FEISHU_APP_SECRET) \
    .log_level(lark.LogLevel.INFO) \
    .build()

feishu = FeishuClient(_lark, config.FEISHU_APP_ID, config.FEISHU_APP_SECRET)
store = SessionStore()
_active_runs = ActiveRunRegistry()

_chat_locks: dict[str, asyncio.Lock] = {}
_queue_gen: dict[str, int] = {}   # 队列代数，flush 时+1，代数不符的消息直接跳过
_MAX_LOCKS = 200

_start_time = time.time()
_last_event = time.time()

# ── 停止处理 ──────────────────────────────────────────────────


async def _announce_stopped(run):
    try:
        await feishu.update_card(run.card_msg_id, "⏹ 已停止当前任务")
    except Exception:
        pass


async def _announce_interrupted(run):
    try:
        await feishu.update_card(run.card_msg_id, "⏹ 已被新消息打断")
    except Exception:
        pass


async def _do_stop(user_id: str) -> str:
    run = _active_runs.get(user_id)
    if run is None:
        return "当前没有正在运行的任务"
    if run.stop_requested:
        return "正在停止，请稍候"
    stopped = await stop_run(_active_runs, user_id, on_stopped=_announce_stopped)
    return "已发送停止请求" if stopped else "当前没有正在运行的任务"


# ── 命令菜单 ──────────────────────────────────────────────────

_MENU_GROUPS = [
    ("**会话**", [
        {"text": "🆕 新会话",      "cmd": "/new"},
        {"text": "📋 规划模式",    "cmd": "/new plan"},
        {"text": "📂 恢复会话",    "cmd": "/resume"},
        {"text": "⏹ 停止任务",     "cmd": "/stop"},
    ]),
    ("**配置**", [
        {"text": "🔄 切模型",      "cmd": "/model"},
        {"text": "⚙️ 切模式",      "cmd": "/mode"},
        {"text": "📁 工作空间",    "cmd": "/ws"},
    ]),
    ("**查看**", [
        {"text": "📊 状态",        "cmd": "/status"},
        {"text": "📈 用量",        "cmd": "/usage"},
        {"text": "🛠 Skills",      "cmd": "/skills"},
        {"text": "🔌 MCP",         "cmd": "/mcp"},
        {"text": "📄 目录",        "cmd": "/ls"},
        {"text": "❓ 帮助",        "cmd": "/help"},
    ]),
]


async def _show_menu(user_id: str, chat_id: str, is_group: bool, msg_id: str):
    elements = []
    for title, buttons in _MENU_GROUPS:
        elements.append({"tag": "markdown", "content": title})
        columns = []
        for btn in buttons:
            value = {"action": "run_cmd", "cmd": btn["cmd"], "cid": chat_id}
            columns.append({
                "tag": "column", "width": "auto",
                "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn["text"]},
                    "type": "default", "size": "small",
                    "name": f"menu_{btn['cmd'].replace('/', '').replace(' ', '_')}",
                    "value": value,
                    "behaviors": [{"type": "callback", "value": value}],
                }],
            })
        elements.append({"tag": "column_set", "flex_mode": "flow", "columns": columns})
    try:
        if is_group:
            card_id = await feishu.reply_card(msg_id, content="⚡ 快捷命令", loading=False)
        else:
            card_id = await feishu.send_card(user_id, content="⚡ 快捷命令", loading=False)
        await feishu.update_card_elements(card_id, elements)
    except Exception as e:
        print(f"[error] 菜单发送失败: {e}", flush=True)


# ── 工具格式化 ────────────────────────────────────────────────

def _fmt_tool(name: str, inp: dict) -> str:
    n = name.lower()
    if n == "bash":
        cmd = inp.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"🔧 **执行命令：** `{cmd}`" if cmd else "🔧 **执行命令...**"
    if n in ("read_file", "read"):
        return f"📄 **读取：** `{inp.get('file_path', inp.get('path', ''))}`"
    if n in ("write_file", "write"):
        return f"✏️ **写入：** `{inp.get('file_path', inp.get('path', ''))}`"
    if n in ("edit_file", "edit"):
        return f"✂️ **编辑：** `{inp.get('file_path', inp.get('path', ''))}`"
    if n == "glob":
        return f"🔍 **搜索文件：** `{inp.get('pattern', '')}`"
    if n == "grep":
        return f"🔎 **搜索内容：** `{inp.get('pattern', '')}`"
    if n == "task":
        return f"🤖 **子任务：** {inp.get('description', inp.get('prompt', ''))[:40]}"
    if n == "webfetch":
        return "🌐 **抓取网页...**"
    if n == "websearch":
        return f"🔍 **搜索：** {inp.get('query', '')}"
    return f"⚙️ **{name}**"


# ── 选项提取 ──────────────────────────────────────────────────

def _extract_options(text: str) -> list[tuple[str, str]]:
    """从文本末尾提取编号选项或 Y/N 选项，返回 [(显示文字, 回复值), ...]。"""
    lines = text.strip().split("\n")
    opts = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            if opts:
                break
            continue
        m = re.match(r"^(\d+|[a-zA-Z])[.）\)、]\s*(.+)", line)
        if m:
            opts.append((m.group(1), m.group(2).strip()))
        elif opts:
            break
        else:
            break
    opts.reverse()
    if len(opts) >= 2:
        return [
            (f"{k}. {d}" if len(d) <= 18 else f"{k}. {d[:16]}..", k)
            for k, d in opts
        ]
    tail = "\n".join(lines[-3:])
    if re.search(r"\by\b.*\bn\b|Y/N|yes.*no|是/否|确认/取消", tail, re.IGNORECASE):
        return [("Yes", "yes"), ("No", "no")]
    return []


# ── Claude 调用 + 流式显示 ────────────────────────────────────

async def _run_and_display(
    user_id: str, chat_id: str, is_group: bool,
    text: str, card_msg_id: str, session, notify_msg_id: str,
):
    run = _active_runs.start(user_id, card_msg_id)

    accumulated = ""
    thinking = ""
    tool_lines: list[str] = []
    ask_options: list[tuple[str, str]] = []
    plan_exited = False
    last_push = 0.0
    push_failures = 0
    _PUSH_INTERVAL = 0.4
    _MAX_DISPLAY = 2500

    async def push(content: str):
        nonlocal push_failures
        if push_failures >= 3:
            return
        try:
            await feishu.update_card(card_msg_id, content)
            push_failures = 0
        except Exception as e:
            push_failures += 1
            print(f"[warn] push 失败 ({push_failures}/3): {e}", flush=True)

    def build_display() -> str:
        parts = []
        if thinking:
            t = thinking if len(thinking) <= 300 else thinking[-300:]
            parts.append(f"💭 **思考中：**\n{t}")
        if tool_lines:
            parts.append("\n".join(tool_lines[-5:]))
        if accumulated:
            if parts:
                parts.append("")
            d = accumulated
            if len(d) > _MAX_DISPLAY:
                d = "...\n\n" + d[-_MAX_DISPLAY:]
            parts.append(d)
        return "\n".join(parts) if parts else "⏳ 思考中..."

    async def on_tool(name: str, inp: dict):
        nonlocal accumulated, last_push, plan_exited
        nl = name.lower()
        if nl == "exitplanmode":
            plan_exited = True
            return
        if nl == "enterplanmode":
            if session.permission_mode != "plan":
                await store.set_permission_mode(user_id, chat_id, "plan")
            return
        if nl == "askuserquestion":
            question = inp.get("question", inp.get("text", ""))
            if question:
                accumulated += f"\n\n❓ **等待回复：**\n{question}"
                detected = _extract_options(question)
                if detected:
                    ask_options.clear()
                    ask_options.extend(detected)
                await push(build_display())
            return
        line = _fmt_tool(name, inp)
        if inp and tool_lines:
            tool_lines[-1] = line
        else:
            tool_lines.append(line)
        await push(build_display())

    async def on_thinking(chunk: str):
        nonlocal thinking, last_push
        thinking += chunk
        now = time.time()
        if now - last_push >= _PUSH_INTERVAL:
            await push(build_display())
            last_push = now

    async def on_chunk(chunk: str):
        nonlocal accumulated, last_push, thinking
        thinking = ""  # 有正文输出时清空思考过程
        accumulated += chunk
        now = time.time()
        if now - last_push >= _PUSH_INTERVAL:
            await push(build_display())
            last_push = now

    try:
        print(f"[run_claude] 开始...", flush=True)
        full_text, new_sid, fallback = await run_claude(
            message=text,
            session_id=session.session_id,
            model=session.model,
            cwd=session.cwd,
            permission_mode=session.permission_mode,
            on_text_chunk=on_chunk,
            on_thinking_chunk=on_thinking,
            on_tool_use=on_tool,
            on_process_start=lambda proc: _active_runs.attach_proc(user_id, proc),
        )
        print(f"[run_claude] 完成, sid={new_sid}", flush=True)
    except Exception as e:
        if run.stop_requested:
            return
        print(f"[error] Claude 失败: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        try:
            await feishu.update_card(card_msg_id, f"❌ Claude 执行出错：{type(e).__name__}: {e}")
        except Exception:
            pass
        return
    finally:
        _active_runs.clear(user_id, run)

    final = full_text or accumulated or "（无输出）"
    if run.stop_requested:
        return
    if fallback:
        final = ("⚠️ 检测到工作目录已变化，旧会话无法继续，本次已自动切换到新 session。\n\n" + final)

    options = _extract_options(final) or ask_options
    try:
        if options:
            buttons = [{"text": display, "value": {"reply": value, "cid": chat_id}}
                       for display, value in options]
            short = all(len(b["text"]) <= 10 for b in buttons)
            await feishu.update_card_with_buttons(card_msg_id, final, buttons, flow=short)
        else:
            await feishu.update_card(card_msg_id, final)
    except Exception as e:
        print(f"[error] 卡片更新失败: {e}", flush=True)
        try:
            if is_group and notify_msg_id:
                await feishu.reply_card(notify_msg_id, content=final, loading=False)
            else:
                await feishu.send_text(user_id, final)
        except Exception as fb_err:
            print(f"[error] 文本回退也失败: {fb_err}", flush=True)

    if new_sid:
        await store.on_claude_response(user_id, chat_id, new_sid, text)

    if plan_exited and session.permission_mode == "plan":
        await store.set_permission_mode(user_id, chat_id, "bypassPermissions")
        notice = "🚀 已退出规划模式，发送任意消息开始执行。"
        try:
            if is_group and notify_msg_id:
                await feishu.reply_text(notify_msg_id, notice)
            else:
                await feishu.send_text(user_id, notice)
        except Exception:
            pass


# ── 消息处理 ──────────────────────────────────────────────────

def _get_lock(chat_id: str) -> asyncio.Lock:
    if len(_chat_locks) >= _MAX_LOCKS:
        idle = [k for k, v in _chat_locks.items() if not v.locked()]
        for k in idle[: len(idle) // 2]:
            del _chat_locks[k]
    return _chat_locks.setdefault(chat_id, asyncio.Lock())


async def handle_message(event: P2ImMessageReceiveV1):
    global _last_event
    _last_event = time.time()

    msg = event.event.message
    sender = event.event.sender
    user_id = sender.sender_id.open_id
    is_group = msg.chat_type == "group"
    chat_id = msg.chat_id if is_group else user_id

    print(f"[消息] type={msg.message_type} group={is_group} "
          f"user={user_id[:8]}... chat={chat_id[:8]}...", flush=True)

    # 文本预处理
    text = ""
    if msg.message_type == "text":
        try:
            text = json.loads(msg.content).get("text", "").strip()
        except Exception:
            return
        if is_group:
            for m in getattr(msg, "mentions", None) or []:
                key = getattr(m, "key", "")
                if key:
                    text = text.replace(key, "").strip()

        # /stop 和 / 在锁外处理
        if text.lower().strip() in ("/stop",) or text.strip().endswith("/stop"):
            reply = await _do_stop(user_id)
            if is_group:
                await feishu.reply_card(msg.message_id, content=reply, loading=False)
            else:
                await feishu.send_card(user_id, content=reply, loading=False)
            return

        if text.strip() in ("/flush", "/clear_queue"):
            _queue_gen[chat_id] = _queue_gen.get(chat_id, 0) + 1
            reply = "🧹 队列已清空，等待中的消息全部丢弃。"
            if is_group:
                await feishu.reply_card(msg.message_id, content=reply, loading=False)
            else:
                await feishu.send_card(user_id, content=reply, loading=False)
            return

        if text == "/":
            await _show_menu(user_id, chat_id, is_group, msg.message_id)
            return

    # 群聊只响应 @mention
    if is_group and not (getattr(msg, "mentions", None) or []):
        return

    # 自动打断逻辑已移除：新消息排队等待，不打断当前任务

    my_gen = _queue_gen.get(chat_id, 0)
    async with _get_lock(chat_id):
        if _queue_gen.get(chat_id, 0) != my_gen:
            return  # 被 /flush 清掉了，直接跳过
        try:
            await _process(user_id, chat_id, is_group, msg)
        except Exception as e:
            print(f"[error] 消息处理异常: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc(file=sys.stdout)


async def _process(user_id: str, chat_id: str, is_group: bool, msg):
    text = ""
    img_path = None

    if msg.message_type == "text":
        try:
            text = json.loads(msg.content).get("text", "").strip()
        except Exception:
            return
        if is_group:
            for m in getattr(msg, "mentions", None) or []:
                key = getattr(m, "key", "")
                if key:
                    text = text.replace(key, "").strip()
        if not text:
            return
        print(f"[文本] {text[:60]}", flush=True)

    elif msg.message_type == "image":
        try:
            image_key = json.loads(msg.content).get("image_key", "")
            if not image_key:
                return
            img_path = await feishu.download_image(msg.message_id, image_key)
            text = f"[用户发送了一张图片，路径：{img_path}，请读取并分析这张图片，用中文回复]"
        except Exception as e:
            err = f"❌ 下载图片失败：{e}"
            if is_group:
                await feishu.reply_card(msg.message_id, content=err, loading=False)
            else:
                await feishu.send_text(user_id, err)
            return
    else:
        return

    # 斜杠命令
    parsed = parse_command(text)
    if parsed:
        cmd, args = parsed
        reply = await handle_command(cmd, args, user_id, chat_id, store)
        if reply is not None:
            if isinstance(reply, dict):
                reply_text = reply["text"]
                reply_btns = reply.get("buttons", [])
            else:
                reply_text, reply_btns = reply, []

            if reply_btns:
                if is_group:
                    card_id = await feishu.reply_card(msg.message_id, content=reply_text, loading=False)
                else:
                    card_id = await feishu.send_card(user_id, content=reply_text, loading=False)
                short = all(len(b["text"]) <= 12 for b in reply_btns)
                try:
                    await feishu.update_card_with_buttons(card_id, reply_text, reply_btns, flow=short)
                except Exception as e:
                    print(f"[warn] 按钮更新失败: {e}", flush=True)
            else:
                if is_group:
                    await feishu.reply_card(msg.message_id, content=reply_text, loading=False)
                else:
                    await feishu.send_card(user_id, content=reply_text, loading=False)
            return
        # reply is None → 转发给 Claude

    # 普通消息 → 调用 Claude
    session = await store.get_current(user_id, chat_id)
    print(f"[Claude] sid={session.session_id} model={session.model}", flush=True)

    try:
        if is_group:
            card_id = await feishu.reply_card(msg.message_id, loading=True)
        else:
            card_id = await feishu.send_card(user_id, loading=True)
    except Exception as e:
        print(f"[error] 占位卡片失败: {e}", flush=True)
        err = f"❌ 发送消息失败：{e}"
        if is_group:
            await feishu.reply_card(msg.message_id, content=err, loading=False)
        else:
            await feishu.send_text(user_id, err)
        return

    await _run_and_display(user_id, chat_id, is_group, text, card_id, session, msg.message_id)


# ── 卡片按钮回调 ──────────────────────────────────────────────

async def _handle_menu_cmd(user_id: str, chat_id: str, cmd_text: str, card_msg_id: str):
    is_group = chat_id != user_id
    parsed = parse_command(cmd_text)
    if not parsed:
        return
    cmd, args = parsed
    if cmd == "stop":
        reply = await _do_stop(user_id)
        if card_msg_id:
            try:
                await feishu.update_card(card_msg_id, reply)
            except Exception:
                pass
        return
    reply = await handle_command(cmd, args, user_id, chat_id, store)
    if reply is None:
        return
    if isinstance(reply, dict):
        text, btns = reply["text"], reply.get("buttons", [])
    else:
        text, btns = reply, []
    if card_msg_id:
        try:
            if btns:
                short = all(len(b["text"]) <= 12 for b in btns)
                await feishu.update_card_with_buttons(card_msg_id, text, btns, flow=short)
            else:
                await feishu.update_card(card_msg_id, text)
        except Exception as e:
            print(f"[error] 菜单命令卡片失败: {e}", flush=True)


async def _handle_resume(user_id: str, chat_id: str, sid: str, card_msg_id: str):
    sid_out, old_title = await store.resume_session(user_id, chat_id, sid)
    if not sid_out:
        return
    name = store.get_summary(user_id, sid_out) or f"#{sid_out[:8]}"
    text = f"✅ 已恢复会话「{name}」，继续对话吧。"
    if old_title:
        text += f"\n上个会话：「{old_title}」"
    if card_msg_id:
        try:
            await feishu.update_card(card_msg_id, text)
        except Exception:
            pass


async def _handle_set_mode(user_id: str, chat_id: str, mode: str, card_msg_id: str):
    await store.set_permission_mode(user_id, chat_id, mode)
    desc = VALID_MODES.get(mode, "")
    if card_msg_id:
        try:
            await feishu.update_card(card_msg_id, f"✅ 已切换为 **{mode}**\n{desc}")
        except Exception:
            pass


async def _handle_btn_reply(user_id: str, chat_id: str, text: str, clicked_msg_id: str):
    is_group = chat_id != user_id

    async with _get_lock(chat_id):
        try:
            session = await store.get_current(user_id, chat_id)
            if is_group and clicked_msg_id:
                card_id = await feishu.reply_card(clicked_msg_id, loading=True)
            else:
                card_id = await feishu.send_card(user_id, loading=True)
            await _run_and_display(user_id, chat_id, is_group, text,
                                   card_id, session, clicked_msg_id or "")
        except Exception as e:
            print(f"[error] 按钮回复失败: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc(file=sys.stdout)


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    global _last_event
    _last_event = time.time()

    event = data.event
    user_id = event.operator.open_id
    value = event.action.value or {}
    action = value.get("action", "")
    chat_id = value.get("cid", user_id)
    clicked_msg_id = (event.context.open_message_id if event.context else None) or ""

    resp = P2CardActionTriggerResponse()
    toast = CallBackToast()

    if action == "set_mode":
        mode = value.get("mode", "")
        if mode:
            asyncio.run_coroutine_threadsafe(
                _handle_set_mode(user_id, chat_id, mode, clicked_msg_id), _bot_loop)
        toast.type, toast.content = "success", f"已切换: {mode}"
    elif action == "run_cmd":
        cmd_text = value.get("cmd", "")
        if cmd_text:
            asyncio.run_coroutine_threadsafe(
                _handle_menu_cmd(user_id, chat_id, cmd_text, clicked_msg_id), _bot_loop)
        toast.type, toast.content = "info", cmd_text
    elif action == "resume_session":
        sid = value.get("sid", "")
        if sid:
            asyncio.run_coroutine_threadsafe(
                _handle_resume(user_id, chat_id, sid, clicked_msg_id), _bot_loop)
        toast.type, toast.content = "info", "正在恢复..."
    else:
        reply_text = value.get("reply", "")
        if reply_text:
            asyncio.run_coroutine_threadsafe(
                _handle_btn_reply(user_id, chat_id, reply_text, clicked_msg_id), _bot_loop)
        toast.type, toast.content = "info", f"已发送: {reply_text}"

    resp.toast = toast
    return resp


def on_message_receive(data: P2ImMessageReceiveV1):
    global _last_event
    _last_event = time.time()
    asyncio.run_coroutine_threadsafe(handle_message(data), _bot_loop)


# ── CLI Handover ──────────────────────────────────────────────

async def _handle_handover(sid: str, cwd: str, model: str,
                            target_user: str, target_chat: str) -> dict:
    user_id = target_user or store.find_primary_user()
    if not user_id:
        return {"ok": False, "error": "找不到用户，请传 user_id 参数"}
    chat_id = target_chat or user_id
    result = await store.handover_session(user_id, chat_id, sid, cwd=cwd, model=model)
    cur = await store.get_current_raw(user_id, chat_id)
    old_note = f"\n上个会话：「{result['old_summary']}」" if result.get("old_summary") else ""
    notice = (
        f"**CLI 会话已接入**\n"
        f"Session: `{sid[:12]}...`\n"
        f"目录: `{cur.get('cwd', '~')}`\n"
        f"模型: `{cur.get('model', '?')}`\n"
        f"模式: `{cur.get('permission_mode', '?')}`{old_note}\n\n"
        "直接发消息即可继续对话。"
    )
    try:
        await feishu.send_card(user_id, content=notice, loading=False)
    except Exception as e:
        print(f"[handover] 推送失败: {e}", flush=True)
    return {"ok": True, "user_id": user_id, "session_id": sid}


# ── HTTP 卡片回调服务器 ────────────────────────────────────────

class _CardHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length))
        except Exception:
            self._resp(400, {"error": "bad json"})
            return

        if data.get("type") == "url_verification":
            self._resp(200, {"challenge": data.get("challenge", "")})
            return

        event = data.get("event", {})
        user_id = event.get("operator", {}).get("open_id", "")
        value = event.get("action", {}).get("value", {})
        ctx = event.get("context", {})
        action = value.get("action", "")
        chat_id = value.get("cid", user_id)
        clicked_msg_id = ctx.get("open_message_id", "")

        print(f"[HTTP回调] user={user_id[:8]}... action={action or 'reply'}", flush=True)

        if action == "set_mode":
            mode = value.get("mode", "")
            if mode:
                asyncio.run_coroutine_threadsafe(
                    _handle_set_mode(user_id, chat_id, mode, clicked_msg_id), _bot_loop)
            self._resp(200, {"toast": {"type": "success", "content": f"已切换: {mode}"}})
        elif action == "run_cmd":
            cmd_text = value.get("cmd", "")
            if cmd_text:
                asyncio.run_coroutine_threadsafe(
                    _handle_menu_cmd(user_id, chat_id, cmd_text, clicked_msg_id), _bot_loop)
            self._resp(200, {"toast": {"type": "info", "content": cmd_text}})
        elif action == "resume_session":
            sid = value.get("sid", "")
            if sid:
                asyncio.run_coroutine_threadsafe(
                    _handle_resume(user_id, chat_id, sid, clicked_msg_id), _bot_loop)
            self._resp(200, {"toast": {"type": "info", "content": "正在恢复..."}})
        else:
            reply_text = value.get("reply", "")
            if reply_text:
                asyncio.run_coroutine_threadsafe(
                    _handle_btn_reply(user_id, chat_id, reply_text, clicked_msg_id), _bot_loop)
            self._resp(200, {"toast": {"type": "info", "content": f"已发送: {reply_text}"}})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/handover":
            params = parse_qs(parsed.query)
            sid = params.get("session_id", [""])[0]
            if not sid:
                self._resp(400, {"error": "session_id required"})
                return
            try:
                future = asyncio.run_coroutine_threadsafe(
                    _handle_handover(
                        sid,
                        params.get("cwd", [""])[0],
                        params.get("model", [""])[0],
                        params.get("user_id", [""])[0],
                        params.get("chat_id", [""])[0],
                    ),
                    _bot_loop,
                )
                self._resp(200, future.result(timeout=15))
            except Exception as e:
                self._resp(500, {"error": str(e)})
        else:
            self._resp(404, {"error": "not found"})

    def _resp(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # 静默 HTTP 日志


# ── 后台任务 ──────────────────────────────────────────────────

def _watchdog():
    MAX_UPTIME = 12 * 3600
    while True:
        time.sleep(300)
        uptime = time.time() - _start_time
        idle = time.time() - _last_event
        if uptime > MAX_UPTIME:
            print(f"[watchdog] 运行 {uptime/3600:.1f}h，定时重启刷新连接", flush=True)
            os._exit(0)
        print(f"[watchdog] uptime={uptime/3600:.1f}h idle={idle/60:.0f}min", flush=True)


def _summary_thread():
    """定期为缺摘要的 session 生成摘要。"""
    from session import generate_summary, write_custom_title
    time.sleep(60)
    while True:
        try:
            unsummarized = store.get_all_unsummarized()
            if unsummarized:
                count = 0
                for uid, sid in unsummarized[:5]:
                    try:
                        summary = generate_summary(sid)
                        if summary:
                            store._data.setdefault(uid, {}).setdefault("summaries", {})[sid] = summary
                            write_custom_title(sid, summary)
                            count += 1
                            print(f"[摘要] #{sid[:8]} → {summary}", flush=True)
                    except Exception as e:
                        print(f"[摘要] #{sid[:8]} 失败: {e}", flush=True)
                    time.sleep(5)
                if count:
                    store._save_sync()
        except Exception as e:
            print(f"[摘要] 定时任务异常: {e}", flush=True)
        time.sleep(600)


def _start_ngrok(port: int) -> str | None:
    import subprocess
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
            for t in json.loads(r.read()).get("tunnels", []):
                if t.get("proto") == "https":
                    return t["public_url"]
    except Exception:
        pass

    try:
        domain = os.environ.get("NGROK_DOMAIN", "")
        cmd = ["ngrok", "http", "--url", domain, str(port)] if domain else ["ngrok", "http", str(port)]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=5) as r:
            for t in json.loads(r.read()).get("tunnels", []):
                if t.get("proto") == "https":
                    return t["public_url"]
    except Exception as e:
        print(f"   [warn] ngrok 启动失败: {e}", flush=True)
    return None


# ── 主入口 ─────────────────────────────────────────────────────

def main():
    print("🚀 飞书 Claude Bot 启动中...")
    print(f"   App ID      : {config.FEISHU_APP_ID}")
    print(f"   默认模型    : {config.DEFAULT_MODEL}")
    print(f"   默认工作目录: {config.DEFAULT_CWD}")
    print(f"   权限模式    : {config.PERMISSION_MODE}")

    port = config.CALLBACK_PORT
    cb_server = HTTPServer(("0.0.0.0", port), _CardHandler)
    threading.Thread(target=cb_server.serve_forever, daemon=True, name="http-callback").start()

    ngrok_url = _start_ngrok(port)
    if ngrok_url:
        print(f"   卡片回调    : {ngrok_url}")
    else:
        print(f"   卡片回调    : http://localhost:{port} (需启动 ngrok)")

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message_receive)
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )
    ws_client = lark.ws.Client(
        config.FEISHU_APP_ID,
        config.FEISHU_APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )

    threading.Thread(target=_watchdog, daemon=True, name="watchdog").start()
    threading.Thread(target=_summary_thread, daemon=True, name="summary").start()

    print("✅ 连接飞书 WebSocket 长连接（自动重连）...")
    ws_client.start()  # 阻塞运行


if __name__ == "__main__":
    main()
