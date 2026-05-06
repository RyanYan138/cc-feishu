# cc-feishu

在飞书私聊/群聊中直接使用 Claude Code CLI，流式卡片输出，支持多会话、斜杠命令和按钮交互。

## 功能特性

- **流式输出** — Claude 响应和思考过程实时更新到飞书卡片，工具调用进度同步显示
- **私聊 + 群聊** — 私聊直接对话；群聊 @机器人 触发响应
- **多会话管理** — 维护多个独立会话，随时切换/恢复，自动生成摘要标题
- **图片理解** — 发图片给机器人，Claude 自动读取分析
- **选项按钮** — Claude 提出选项时自动生成卡片按钮，点击即回复
- **消息队列** — 多条消息自动排队，用 `/flush` 可一键清空队列
- **工作空间** — 保存常用目录，群组可绑定独立工作空间

## 前置条件

- Python 3.11+
- [Claude Code CLI](https://claude.ai/code) 已安装并登录（`claude` 命令可用）
- 飞书自建应用（需开通消息读写、卡片交互等权限）

## 快速开始

**1. 安装依赖**

```bash
pip install -r requirements.txt
```

**2. 配置环境变量**

```bash
cp .env.example .env
```

编辑 `.env`：

```env
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DEFAULT_MODEL=claude-sonnet-4-6
DEFAULT_CWD=~
PERMISSION_MODE=bypassPermissions
```

**3. 启动**

```bash
python main.py
```

## 飞书应用配置

在 [飞书开放平台](https://open.feishu.cn/app) 创建自建应用，配置以下内容：

**权限（机器人能力 → 权限管理）**

| 权限 | 说明 |
|------|------|
| `im:message` | 读取/发送消息 |
| `im:message.group_at_msg` | 接收群聊 @消息 |
| `im:resource` | 下载图片等资源 |

**事件订阅**

开启长连接模式（不需要公网 URL），订阅以下事件：
- `im.message.receive_v1` — 接收消息

**卡片回调（可选，按钮交互需要）**

```bash
ngrok http 9981
```

将生成的 `https://xxx.ngrok-free.app` 填入飞书应用 → 卡片搭建 → 卡片回调地址。

如有固定域名，在 `.env` 中设置：
```env
NGROK_DOMAIN=your-domain.ngrok-free.app
```

---

## 命令完整说明

发送 `/` 可召唤快捷菜单。

### 会话管理

Claude Code 的每个 session 对应一个工作目录下的对话历史。切换目录后建议用 `/new` 开新 session，Claude 才真正在新目录下工作。

| 命令 | 说明 |
|------|------|
| `/new` | 开始新 session，保留当前模型和目录设置 |
| `/new plan` | 以规划模式（只规划不执行工具）开始新 session |
| `/clear` | 同 `/new` |
| `/resume` | 列出所有历史 session（含摘要和时间），点击按钮恢复 |
| `/resume 2` | 直接恢复列表中第 2 条 session |
| `/resume <session_id>` | 按完整 ID 恢复指定 session |
| `/stop` | 停止当前正在运行的任务 |
| `/flush` | 清空队列中所有等待处理的消息 |
| `/status` | 查看当前 session ID、模型、目录、权限模式 |

> `/resume` 会列出所有目录下的历史 session，不限于当前目录。

---

### 模型切换

| 命令 | 说明 |
|------|------|
| `/model` | 显示当前模型，弹出 Opus / Sonnet / Haiku 切换按钮 |
| `/model opus` | 切换为 claude-opus-4-6 |
| `/model sonnet` | 切换为 claude-sonnet-4-6 |
| `/model haiku` | 切换为 claude-haiku-4-5-20251001 |
| `/model <完整ID>` | 切换为任意指定模型 |

---

### 权限模式

控制 Claude 执行工具时是否需要确认。

| 命令 | 说明 |
|------|------|
| `/mode` | 显示当前模式，弹出切换按钮 |
| `/mode bypassPermissions` | 全自动，无需任何确认（个人使用推荐） |
| `/mode acceptEdits` | 文件编辑自动接受，其余需确认 |
| `/mode plan` | 只输出规划，不执行任何工具 |
| `/mode default` | 每次工具调用都需手动确认 |

---

### 目录与工作空间

Claude 执行工具时的工作目录，决定了它能操作哪个项目的文件。

**临时切换目录：**

```
/cd F:\CodeProject\my-project
/new
```

**保存工作空间（推荐）：**

```
/ws save feishu F:\CodeProject\cc-feishu    # 保存
/ws save ragflow F:\CodeProject\ragflow      # 保存另一个

/ws use feishu   # 切换到 cc-feishu 目录
/new             # 开新 session，Claude 在该目录工作
```

**群组隔离：**  
不同群组可以绑定不同工作空间，互不干扰。A 群用 feishu，B 群用 ragflow，各自独立。

**完整命令列表：**

| 命令 | 说明 |
|------|------|
| `/ws` | 查看全部工作空间，点击按钮切换 |
| `/ws save <名称> [路径]` | 保存工作空间，不填路径则保存当前目录 |
| `/ws use <名称>` | 当前群组切换到指定工作空间 |
| `/ws set <路径>` | 直接设置目录（不保存到工作空间） |
| `/ws remove <名称>` | 删除工作空间 |
| `/cd <路径>` | 临时切换目录（不保存） |
| `/ls` | 列出当前工作目录内容 |
| `/ls <路径>` | 列出指定路径内容 |

---

### 查看与工具

| 命令 | 说明 |
|------|------|
| `/skills` | 列出已安装的 Claude Skills，点击按钮执行 |
| `/mcp` | 列出已配置的 MCP Servers |
| `/usage` | 查看 Claude Max 订阅 5小时/7天 用量百分比和重置时间 |
| `/help` | 显示帮助 |
| `/` | 弹出快捷命令菜单 |

---

### 转发给 Claude 的命令

不在上述列表中的 `/xxx` 命令（如 `/commit`、`/review`、`/init` 等）会直接转发给 Claude 执行，等价于对 Claude 说出这条命令。

---

## CLI Handover

在终端启动 Claude Code 会话后，可以将该会话接入飞书继续对话：

```bash
curl "http://localhost:9981/handover?session_id=SESSION_ID&cwd=/path&model=claude-sonnet-4-6"
```

飞书收到通知后，直接发消息即可继续该 session。

---

## 项目结构

```
cc-feishu/
├── main.py         # 主入口：飞书 WebSocket + 消息/卡片事件处理
├── config.py       # 配置（读取 .env）
├── feishu.py       # 飞书 API 封装（发送/更新卡片、下载图片）
├── claude.py       # claude CLI 调用 + stream-json 流式解析
├── session.py      # 会话存储（持久化到 ~/.cc-feishu/sessions.json）
├── commands.py     # 斜杠命令处理
├── run_control.py  # 活跃任务跟踪（/stop、自动打断）
├── requirements.txt
└── .env.example
```

---

## 常见问题

**Q: 群聊里机器人没有响应**

确认消息中有 @机器人，且应用已开通 `im:message.group_at_msg` 权限并订阅了消息事件。

**Q: 卡片按钮点击没有反应**

需要配置 ngrok 并将回调地址填入飞书应用。纯文字交互不需要 ngrok。

**Q: `/usage` 显示找不到凭证**

需要先在终端执行 `claude` 命令完成登录，登录后凭证保存在 `~/.claude/.credentials.json`。

**Q: Windows 下命令报错**

确保使用最新代码。本项目已针对 Windows 的 `.cmd` 文件调用做了兼容处理。

---

## 致谢

本项目受 [feishu-claude-code](https://github.com/joewongjc/feishu-claude-code) 启发，借鉴了其飞书 WebSocket 接入、流式卡片更新、会话管理和斜杠命令体系等核心设计。感谢原作者 [@joewongjc](https://github.com/joewongjc) 的开源贡献。

在原项目基础上，本版本针对 Windows 兼容性进行了重写，修复了 `.cmd` 文件调用、跨平台子进程检测、`stdout` 空值等问题，并对代码结构做了整体梳理。
