"""业务运行上限；DeepSeek 与 DataPro 密钥分别由各自的请求模块读取。"""

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_DIR / "runtime" / "agent.sqlite3"
DEFAULT_OUTPUT = PROJECT_DIR / "runtime" / "ledger"
KEY_FILE = Path(os.environ.get("SANJING_KEY_FILE", "F:/#2642Agent/DeepSeekKey（UTF8）.txt"))
MODEL_NAME = os.environ.get("SANJING_MODEL", "deepseek-flash")
API_URL = "https://api.deepseek.com/chat/completions"
MAX_STEPS = 8
TIMEOUT_SECONDS = 60
MAX_OUTPUT_TOKENS = 1500
CONTEXT_CHAR_BUDGET = 48000
MAX_USER_CHARS = 4000
MAX_TOOL_CALLS_PER_STEP = 8
MAX_DATA_REQUESTS = 16
DATA_TIMEOUT_SECONDS = 60
TURN_TIMEOUT_SECONDS = 300

