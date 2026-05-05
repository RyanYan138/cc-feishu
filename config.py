import os
import shutil
import sys
from dotenv import load_dotenv

load_dotenv()

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]

CLAUDE_CLI = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"


def claude_cmd(*args) -> list[str]:
    """构造 claude CLI 命令列表，Windows 上的 .cmd 文件需要通过 cmd /c 调用。"""
    if sys.platform == "win32" and CLAUDE_CLI.lower().endswith(".cmd"):
        return ["cmd", "/c", CLAUDE_CLI, *args]
    return [CLAUDE_CLI, *args]

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-sonnet-4-6")
DEFAULT_CWD = os.path.expanduser(os.getenv("DEFAULT_CWD", "~"))
PERMISSION_MODE = os.getenv("PERMISSION_MODE", "bypassPermissions")

SESSIONS_DIR = os.path.expanduser("~/.cc-feishu")

CALLBACK_PORT = int(os.getenv("CALLBACK_PORT", "9981"))
