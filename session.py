"""
会话存储：管理每个用户/群组的 Claude session ID、模型、工作目录、历史记录。
数据持久化到 ~/.cc-feishu/sessions.json，使用原子写入防止损坏。
"""

import asyncio
import json
import os
import re
import ssl
import subprocess
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional

from config import SESSIONS_DIR, DEFAULT_MODEL, DEFAULT_CWD, PERMISSION_MODE

SESSIONS_FILE = os.path.join(SESSIONS_DIR, "sessions.json")
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")


# ── Session 文件扫描工具 ──────────────────────────────────────

def _clean_text(text: str) -> str:
    """去除 CLI 注入的系统内容，保留用户原始文本。"""
    text = re.sub(r"^\[环境：[^\]]*\]\s*", "", text)
    text = re.sub(r"<[a-z_-]+>.*?</[a-z_-]+>\s*", "", text, flags=re.DOTALL)
    return text.strip()


def _parse_session_file(fpath: str, sid: str, mtime: float) -> dict:
    """从 .jsonl 文件提取首条用户消息作为预览。"""
    preview, cwd = "", ""
    started_at = datetime.fromtimestamp(mtime).isoformat()
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "user":
                    continue
                if not cwd and d.get("cwd"):
                    cwd = d["cwd"]
                if d.get("timestamp"):
                    started_at = d["timestamp"][:19].replace("T", " ")
                msg = d.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    text = " ".join(b.get("text", "") for b in content
                                    if b.get("type") == "text").strip()
                else:
                    text = str(content).strip()
                text = _clean_text(text)
                if text:
                    preview = text[:50]
                    break
    except OSError:
        pass
    return {"session_id": sid, "started_at": started_at, "cwd": cwd, "preview": preview}


def scan_cli_sessions(limit: int = 30) -> list[dict]:
    """扫描 ~/.claude/projects/ 下所有 session，按最近修改时间倒序。"""
    if not os.path.isdir(CLAUDE_PROJECTS):
        return []
    entries = []
    for proj_dir in os.listdir(CLAUDE_PROJECTS):
        proj_path = os.path.join(CLAUDE_PROJECTS, proj_dir)
        if not os.path.isdir(proj_path):
            continue
        for fname in os.listdir(proj_path):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(proj_path, fname)
            entries.append((os.path.getmtime(fpath), fname[:-6], fpath))
    entries.sort(key=lambda x: x[0], reverse=True)
    return [_parse_session_file(p, s, m) for m, s, p in entries[:limit]]


def _find_session_file(sid: str) -> Optional[str]:
    if not os.path.isdir(CLAUDE_PROJECTS):
        return None
    for proj_dir in os.listdir(CLAUDE_PROJECTS):
        fpath = os.path.join(CLAUDE_PROJECTS, proj_dir, f"{sid}.jsonl")
        if os.path.isfile(fpath):
            return fpath
    return None


def _get_api_token() -> Optional[str]:
    """读取 Claude oauth token（先试凭证文件，再试 macOS keychain）。"""
    try:
        creds_path = os.path.expanduser("~/.claude/.credentials.json")
        if os.path.isfile(creds_path):
            with open(creds_path) as f:
                return json.load(f)["claudeAiOauth"]["accessToken"]
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        return json.loads(result.stdout.strip())["claudeAiOauth"]["accessToken"]
    except Exception:
        return None


def generate_summary(sid: str, token: Optional[str] = None) -> str:
    """用 claude haiku 为 session 生成一句话摘要（同步，在线程中调用）。"""
    fpath = _find_session_file(sid)
    if not fpath:
        return ""

    parts = []
    total = 0
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if d.get("type") not in ("user", "assistant") or d.get("isMeta"):
                    continue
                msg = d.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    text = " ".join(b.get("text", "") for b in content
                                    if b.get("type") == "text").strip()
                else:
                    text = str(content).strip()
                text = _clean_text(text)
                if not text:
                    continue
                role = "用户" if d["type"] == "user" else "助手"
                parts.append(f"{role}: {text}")
                total += len(parts[-1])
                if total >= 2000:
                    break
    except OSError:
        return ""

    if not parts:
        return ""

    if token is None:
        token = _get_api_token()
    if not token:
        return ""

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 40,
        "messages": [{"role": "user", "content": (
            "用10-20个中文字总结这段对话的主题。"
            "直接返回摘要，不加引号不加标点。\n\n"
            + "\n".join(parts)[:2000]
        )}],
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
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            blocks = json.loads(resp.read()).get("content", [])
            if blocks and blocks[0].get("type") == "text":
                return blocks[0]["text"].strip()
    except Exception as e:
        print(f"[摘要] {sid[:8]} 生成失败: {e}", flush=True)
    return ""


def write_custom_title(sid: str, title: str):
    """将摘要作为 custom-title 写入 .jsonl，让 CLI 终端也能显示。"""
    fpath = _find_session_file(sid)
    if not fpath:
        return
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    if json.loads(raw.strip()).get("type") == "custom-title":
                        return
                except Exception:
                    continue
        with open(fpath, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "custom-title", "customTitle": title, "sessionId": sid,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ── Session 数据模型 ──────────────────────────────────────────

class Session:
    __slots__ = ("session_id", "model", "cwd", "permission_mode", "workspace")

    def __init__(self, session_id, model, cwd, permission_mode, workspace=""):
        self.session_id = session_id
        self.model = model
        self.cwd = cwd
        self.permission_mode = permission_mode
        self.workspace = workspace


class SessionStore:
    def __init__(self):
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        self._data: dict = self._load()
        self._save_lock = asyncio.Lock()
        self._dedup_histories()

    # ── 持久化 ─────────────────────────────────────────────────

    def _load(self) -> dict:
        if os.path.exists(SESSIONS_FILE):
            try:
                with open(SESSIONS_FILE, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_sync(self):
        tmp = SESSIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SESSIONS_FILE)

    async def _save(self):
        async with self._save_lock:
            await asyncio.to_thread(self._save_sync)

    # ── 内部工具 ───────────────────────────────────────────────

    def _dedup_histories(self):
        changed = False
        for user in self._data.values():
            for chat_data in user.values():
                if not isinstance(chat_data, dict) or "history" not in chat_data:
                    continue
                seen, cleaned = set(), []
                for h in reversed(chat_data["history"]):
                    sid = h.get("session_id")
                    if sid and sid not in seen:
                        seen.add(sid)
                        cleaned.append(h)
                cleaned.reverse()
                if len(cleaned) != len(chat_data["history"]):
                    chat_data["history"] = cleaned
                    changed = True
        if changed:
            self._save_sync()

    def _chat_key(self, user_id: str, chat_id: str) -> str:
        return "private" if chat_id == user_id else chat_id

    def _default_current(self) -> dict:
        return {
            "session_id": None,
            "model": DEFAULT_MODEL,
            "cwd": DEFAULT_CWD,
            "permission_mode": PERMISSION_MODE,
            "started_at": datetime.now().isoformat(),
            "preview": "",
            "workspace": "",
        }

    async def _chat_data(self, user_id: str, chat_id: str) -> dict:
        user = self._data.setdefault(user_id, {})
        key = self._chat_key(user_id, chat_id)
        changed = False

        if key not in user:
            # 兼容旧数据结构：把顶层 current/history 迁入 private
            if key == "private" and isinstance(user.get("current"), dict):
                user[key] = {"current": user.pop("current"), "history": user.pop("history", [])}
            else:
                user[key] = {"current": self._default_current(), "history": []}
            changed = True

        chat = user[key]
        cur = chat.setdefault("current", self._default_current())
        for k, v in self._default_current().items():
            if k not in cur:
                cur[k] = v
                changed = True
        chat.setdefault("history", [])

        if changed:
            await self._save()
        return chat

    async def _bg_summarize(self, user_id: str, sid: str):
        """后台生成并缓存摘要，不阻塞消息流。"""
        try:
            summary = await asyncio.to_thread(generate_summary, sid)
            if summary:
                self._data[user_id].setdefault("summaries", {})[sid] = summary
                await asyncio.to_thread(write_custom_title, sid, summary)
                await self._save()
        except Exception:
            pass

    # ── 公开接口 ───────────────────────────────────────────────

    def get_summary(self, user_id: str, sid: str) -> str:
        return self._data.get(user_id, {}).get("summaries", {}).get(sid, "")

    def get_all_unsummarized(self) -> list[tuple[str, str]]:
        results = []
        for uid, udata in self._data.items():
            summaries = udata.get("summaries", {})
            for chat_data in udata.values():
                if not isinstance(chat_data, dict) or "history" not in chat_data:
                    continue
                cur_sid = chat_data.get("current", {}).get("session_id")
                if cur_sid and not summaries.get(cur_sid):
                    results.append((uid, cur_sid))
                for h in chat_data.get("history", []):
                    if h.get("session_id") and not summaries.get(h["session_id"]):
                        results.append((uid, h["session_id"]))
        return results

    async def get_current(self, user_id: str, chat_id: str) -> Session:
        cur = await self.get_current_raw(user_id, chat_id)
        return Session(
            session_id=cur.get("session_id"),
            model=cur.get("model", DEFAULT_MODEL),
            cwd=cur.get("cwd", DEFAULT_CWD),
            permission_mode=cur.get("permission_mode", PERMISSION_MODE),
            workspace=cur.get("workspace", ""),
        )

    async def get_current_raw(self, user_id: str, chat_id: Optional[str] = None) -> dict:
        if chat_id is None:
            chat_id = user_id
        return (await self._chat_data(user_id, chat_id))["current"]

    async def on_claude_response(self, user_id: str, chat_id: str,
                                  new_sid: str, first_msg: str):
        """Claude 回复后更新 session_id，必要时归档旧 session。"""
        chat = await self._chat_data(user_id, chat_id)
        cur = chat["current"]
        old_sid = cur.get("session_id")

        if old_sid and old_sid != new_sid:
            chat["history"] = [h for h in chat["history"] if h["session_id"] != old_sid]
            chat["history"].append({
                "session_id": old_sid,
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            chat["history"] = chat["history"][-20:]
            cur["started_at"] = datetime.now().isoformat()
            if not self._data[user_id].get("summaries", {}).get(old_sid):
                asyncio.create_task(self._bg_summarize(user_id, old_sid))

        cur["session_id"] = new_sid
        if not cur.get("preview"):
            cur["preview"] = _clean_text(first_msg)[:40]
        await self._save()

    async def new_session(self, user_id: str, chat_id: str) -> str:
        """开始新 session，归档当前，返回旧 session 的摘要标题。"""
        chat = await self._chat_data(user_id, chat_id)
        cur = chat["current"]
        old_title = ""

        if cur.get("session_id"):
            old_id = cur["session_id"]
            chat["history"] = [h for h in chat["history"] if h["session_id"] != old_id]
            chat["history"].append({
                "session_id": old_id,
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            chat["history"] = chat["history"][-20:]
            old_title = self.get_summary(user_id, old_id)
            if not old_title:
                asyncio.create_task(self._bg_summarize(user_id, old_id))

        chat["current"] = {
            "session_id": None,
            "model": cur.get("model", DEFAULT_MODEL),
            "cwd": cur.get("cwd", DEFAULT_CWD),
            "permission_mode": cur.get("permission_mode", PERMISSION_MODE),
            "started_at": datetime.now().isoformat(),
            "preview": "",
            "workspace": cur.get("workspace", ""),
        }
        await self._save()
        return old_title

    async def set_model(self, user_id: str, chat_id: str, model: str):
        (await self._chat_data(user_id, chat_id))["current"]["model"] = model
        await self._save()

    async def set_cwd(self, user_id: str, chat_id: str,
                      cwd: str, workspace_name: str = ""):
        cur = (await self._chat_data(user_id, chat_id))["current"]
        cur["cwd"] = cwd
        cur["workspace"] = workspace_name
        await self._save()

    async def set_permission_mode(self, user_id: str, chat_id: str, mode: str):
        (await self._chat_data(user_id, chat_id))["current"]["permission_mode"] = mode
        await self._save()

    async def resume_session(self, user_id: str, chat_id: str,
                              index_or_id: str) -> tuple[Optional[str], str]:
        """按序号（1-based）或 session_id 恢复会话，返回 (session_id, old_title)。"""
        if user_id not in self._data:
            return None, ""
        key = self._chat_key(user_id, chat_id)
        if key not in self._data[user_id]:
            return None, ""

        chat = await self._chat_data(user_id, chat_id)
        history = chat.get("history", [])

        try:
            idx = int(index_or_id) - 1
            if 0 <= idx < len(history):
                target_sid = history[idx]["session_id"]
            else:
                return None, ""
        except ValueError:
            target_sid = index_or_id

        cur = chat["current"]
        old_id = cur.get("session_id")
        old_title = ""
        if old_id and old_id != target_sid:
            chat["history"] = [h for h in history if h["session_id"] != old_id]
            chat["history"].append({
                "session_id": old_id,
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            chat["history"] = chat["history"][-20:]
            old_title = self.get_summary(user_id, old_id)
            if not old_title:
                asyncio.create_task(self._bg_summarize(user_id, old_id))

        orig_preview = next(
            (h.get("preview", "") for h in chat["history"] if h["session_id"] == target_sid), ""
        )
        orig_started = next(
            (h.get("started_at", "") for h in chat["history"] if h["session_id"] == target_sid), ""
        )
        cur["session_id"] = target_sid
        cur["preview"] = orig_preview
        cur["started_at"] = orig_started or datetime.now().isoformat()
        await self._save()
        return target_sid, old_title

    async def list_sessions(self, user_id: str, chat_id: str) -> list:
        if user_id not in self._data:
            return []
        key = self._chat_key(user_id, chat_id)
        if key not in self._data[user_id]:
            return []
        return list(reversed((await self._chat_data(user_id, chat_id)).get("history", [])))

    def list_workspaces(self, user_id: str) -> dict[str, str]:
        return dict(sorted(self._data.get(user_id, {}).get("workspaces", {}).items()))

    async def save_workspace(self, user_id: str, name: str, cwd: str):
        self._data.setdefault(user_id, {}).setdefault("workspaces", {})[name] = cwd
        await self._save()

    async def delete_workspace(self, user_id: str, name: str) -> bool:
        user = self._data.get(user_id, {})
        if name not in user.get("workspaces", {}):
            return False
        del user["workspaces"][name]
        for chat_data in user.values():
            if isinstance(chat_data, dict) and "current" in chat_data:
                if chat_data["current"].get("workspace") == name:
                    chat_data["current"]["workspace"] = ""
        await self._save()
        return True

    async def bind_workspace(self, user_id: str, chat_id: str, name: str) -> Optional[str]:
        path = self._data.get(user_id, {}).get("workspaces", {}).get(name)
        if not path:
            return None
        await self.set_cwd(user_id, chat_id, path, workspace_name=name)
        return path

    async def handover_session(self, user_id: str, chat_id: str, sid: str,
                                cwd: str = "", model: str = "") -> dict:
        """CLI handover：将指定 session 设为当前会话。"""
        chat = await self._chat_data(user_id, chat_id)
        cur = chat["current"]
        old_sid = cur.get("session_id")
        old_summary = ""

        if old_sid and old_sid != sid:
            chat["history"] = [h for h in chat["history"] if h["session_id"] != old_sid]
            chat["history"].append({
                "session_id": old_sid,
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            chat["history"] = chat["history"][-20:]
            old_summary = self.get_summary(user_id, old_sid)
            if not old_summary:
                asyncio.create_task(self._bg_summarize(user_id, old_sid))

        cur["session_id"] = sid
        cur["started_at"] = datetime.now().isoformat()
        cur["preview"] = ""
        if cwd:
            cur["cwd"] = cwd
        if model:
            cur["model"] = model
        await self._save()
        return {"old_session_id": old_sid or "", "old_summary": old_summary}

    def find_primary_user(self) -> Optional[str]:
        for uid in self._data:
            if uid.startswith("ou_") and "private" in self._data[uid]:
                return uid
        return None
