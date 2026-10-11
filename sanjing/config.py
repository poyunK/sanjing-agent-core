"""业务运行上限；各供应商密钥由各自的请求模块读取。"""

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_DIR / "runtime" / "agent.sqlite3"
DEFAULT_OUTPUT = PROJECT_DIR / "runtime" / "ledger"
KEY_FILE = Path(os.environ.get("SANJING_KEY_FILE", "F:/#2642Agent/DeepSeekKey（UTF8）.txt"))
ARK_KEY_FILE = Path(os.environ.get(
    "SANJING_ARK_KEY_FILE", "C:/Users/pla53519/Documents/火山key.yaml"))
PROVIDER_NAME = os.environ.get("SANJING_PROVIDER", "ark").strip().lower()
_DEFAULT_MODELS = {"ark": "ark-code-latest", "deepseek": "deepseek-flash"}
MODEL_NAME = os.environ.get("SANJING_MODEL", _DEFAULT_MODELS.get(PROVIDER_NAME, "ark-code-latest"))
ARK_API_URL = "https://ark.cn-beijing.volces.com/api/plan/v3/responses"
API_URL = "https://api.deepseek.com/chat/completions"
ARK_CONTEXT_TOKENS = 256_000
ARK_MAX_OUTPUT_TOKENS = 32_000
MAX_STEPS = 8
TIMEOUT_SECONDS = 60
MAX_OUTPUT_TOKENS = 1500
try:
    _configured_output_tokens = int(os.environ.get("SANJING_MAX_OUTPUT_TOKENS", "1500"))
    if _configured_output_tokens > 0:
        MAX_OUTPUT_TOKENS = min(ARK_MAX_OUTPUT_TOKENS, _configured_output_tokens)
except (TypeError, ValueError):
    pass
CONTEXT_CHAR_BUDGET = 48000
MAX_USER_CHARS = 4000
MAX_TOOL_CALLS_PER_STEP = 8
MAX_DATA_REQUESTS = 16
MAX_SEARCH_REQUESTS = 4
DATA_TIMEOUT_SECONDS = 60
TURN_TIMEOUT_SECONDS = 300

