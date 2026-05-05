"""
飞书 API 异步封装：发送/更新卡片消息、下载图片。
"""

import asyncio
import json
import os
import tempfile
import time
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1.model import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

_MAX_CHUNK = 2800  # 飞书 markdown 元素单块字符上限（保守值）


def _make_card(content: str = "", loading: bool = False) -> str:
    """构造飞书卡片 JSON（Card JSON 2.0）。内容过长时自动分段。"""
    if loading:
        elements = [{"tag": "markdown", "content": "⏳ 思考中..."}]
    elif len(content) <= _MAX_CHUNK:
        elements = [{"tag": "markdown", "content": content}]
    else:
        elements = []
        current = ""
        idx = 0
        for line in content.split("\n"):
            if len(line) > _MAX_CHUNK:
                if current:
                    elements.append({"tag": "markdown", "content": current})
                    current = ""
                for i in range(0, len(line), _MAX_CHUNK):
                    elements.append({"tag": "markdown", "content": line[i:i + _MAX_CHUNK]})
                idx += len(range(0, len(line), _MAX_CHUNK))
                continue
            if len(current) + len(line) + 1 > _MAX_CHUNK:
                elements.append({"tag": "markdown", "content": current})
                prefix = f"**（续 {idx}）**\n\n" if idx > 0 else ""
                current = prefix + line
                idx += 1
            else:
                current = current + "\n" + line if current else line
        if current:
            elements.append({"tag": "markdown", "content": current})

    return json.dumps({"schema": "2.0", "body": {"elements": elements}}, ensure_ascii=False)


class FeishuClient:
    def __init__(self, client: lark.Client, app_id: str, app_secret: str):
        self._client = client
        self._app_id = app_id
        self._app_secret = app_secret

    async def _retry(self, fn, retries: int = 3, delay: float = 0.5):
        last_err = None
        for attempt in range(retries):
            try:
                return await fn()
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    print(f"[retry] {attempt+1}/{retries} 失败，{delay:.1f}s 后重试: {e}", flush=True)
                    await asyncio.sleep(delay)
                    delay *= 2
        raise last_err

    # ── 发送消息 ──────────────────────────────────────────────

    async def send_card(self, open_id: str, content: str = "", loading: bool = True) -> str:
        """向用户发送卡片消息，返回 message_id。"""
        async def _do():
            req = (CreateMessageRequest.builder()
                   .receive_id_type("open_id")
                   .request_body(CreateMessageRequestBody.builder()
                                 .receive_id(open_id)
                                 .msg_type("interactive")
                                 .content(_make_card(content, loading=loading))
                                 .build())
                   .build())
            resp = await self._client.im.v1.message.acreate(req)
            if not resp.success():
                raise RuntimeError(f"send_card failed: {resp.code} {resp.msg}")
            return resp.data.message_id
        return await self._retry(_do)

    async def reply_card(self, msg_id: str, content: str = "", loading: bool = True) -> str:
        """回复消息（卡片），返回新消息 message_id。"""
        async def _do():
            req = (ReplyMessageRequest.builder()
                   .message_id(msg_id)
                   .request_body(ReplyMessageRequestBody.builder()
                                 .msg_type("interactive")
                                 .content(_make_card(content, loading=loading))
                                 .build())
                   .build())
            resp = await self._client.im.v1.message.areply(req)
            if not resp.success():
                raise RuntimeError(f"reply_card failed: {resp.code} {resp.msg}")
            return resp.data.message_id
        return await self._retry(_do)

    async def update_card(self, msg_id: str, content: str):
        """更新已发送卡片的内容。"""
        async def _do():
            req = (PatchMessageRequest.builder()
                   .message_id(msg_id)
                   .request_body(PatchMessageRequestBody.builder()
                                 .content(_make_card(content))
                                 .build())
                   .build())
            resp = await self._client.im.v1.message.apatch(req)
            if not resp.success():
                raise RuntimeError(f"update_card failed: {resp.code} {resp.msg}")
        await self._retry(_do)

    async def update_card_with_buttons(self, msg_id: str, content: str,
                                       buttons: list[dict], flow: bool = False):
        """更新卡片并在底部附加操作按钮。flow=True 横排，False 竖排。"""
        card = json.loads(_make_card(content))
        btn_els = []
        for i, btn in enumerate(buttons):
            btn_els.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn["text"]},
                "type": "default",
                "size": "small",
                "name": f"btn_{i}",
                "value": btn["value"],
                "behaviors": [{"type": "callback", "value": btn["value"]}],
            })
        if flow and btn_els:
            card["body"]["elements"].append({
                "tag": "column_set",
                "flex_mode": "flow",
                "columns": [{"tag": "column", "width": "auto", "elements": [b]} for b in btn_els],
            })
        else:
            card["body"]["elements"].extend(btn_els)

        card_json = json.dumps(card, ensure_ascii=False)

        async def _do():
            req = (PatchMessageRequest.builder()
                   .message_id(msg_id)
                   .request_body(PatchMessageRequestBody.builder().content(card_json).build())
                   .build())
            resp = await self._client.im.v1.message.apatch(req)
            if not resp.success():
                raise RuntimeError(f"update_card_with_buttons failed: {resp.code} {resp.msg}")
        await self._retry(_do)

    async def update_card_elements(self, msg_id: str, elements: list[dict]):
        """用自定义 elements 更新卡片（markdown + button 混排）。"""
        card_json = json.dumps({"schema": "2.0", "body": {"elements": elements}}, ensure_ascii=False)

        async def _do():
            req = (PatchMessageRequest.builder()
                   .message_id(msg_id)
                   .request_body(PatchMessageRequestBody.builder().content(card_json).build())
                   .build())
            resp = await self._client.im.v1.message.apatch(req)
            if not resp.success():
                raise RuntimeError(f"update_card_elements failed: {resp.code} {resp.msg}")
        await self._retry(_do)

    async def reply_text(self, msg_id: str, text: str) -> str:
        """回复纯文本消息。"""
        async def _do():
            req = (ReplyMessageRequest.builder()
                   .message_id(msg_id)
                   .request_body(ReplyMessageRequestBody.builder()
                                 .msg_type("text")
                                 .content(json.dumps({"text": text}))
                                 .build())
                   .build())
            resp = await self._client.im.v1.message.areply(req)
            if not resp.success():
                raise RuntimeError(f"reply_text failed: {resp.code} {resp.msg}")
            return resp.data.message_id
        return await self._retry(_do, retries=2)

    async def send_text(self, open_id: str, text: str) -> str:
        """向用户发送纯文本消息。"""
        req = (CreateMessageRequest.builder()
               .receive_id_type("open_id")
               .request_body(CreateMessageRequestBody.builder()
                             .receive_id(open_id)
                             .msg_type("text")
                             .content(json.dumps({"text": text}))
                             .build())
               .build())
        resp = await self._client.im.v1.message.acreate(req)
        if not resp.success():
            raise RuntimeError(f"send_text failed: {resp.code} {resp.msg}")
        return resp.data.message_id

    async def download_image(self, msg_id: str, image_key: str) -> str:
        """下载飞书图片到临时文件，返回本地路径。"""
        return await asyncio.to_thread(self._download_image_sync, msg_id, image_key)

    def _download_image_sync(self, msg_id: str, image_key: str) -> str:
        import ssl
        import urllib.request
        import uuid

        ctx = ssl.create_default_context()

        # 获取 token
        token_body = json.dumps({"app_id": self._app_id, "app_secret": self._app_secret}).encode()
        token_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=token_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, context=ctx, timeout=10) as r:
            token = json.loads(r.read())["tenant_access_token"]

        url = (f"https://open.feishu.cn/open-apis/im/v1/messages"
               f"/{msg_id}/resources/{image_key}?type=image")
        img_req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        tmp_path = os.path.join(tempfile.gettempdir(), f"feishu-img-{uuid.uuid4().hex[:8]}.jpg")
        with urllib.request.urlopen(img_req, context=ctx, timeout=15) as r:
            ct = r.headers.get("Content-Type", "")
            if "png" in ct:
                tmp_path = tmp_path.replace(".jpg", ".png")
            elif "gif" in ct:
                tmp_path = tmp_path.replace(".jpg", ".gif")
            with open(tmp_path, "wb") as f:
                f.write(r.read())
        return tmp_path
