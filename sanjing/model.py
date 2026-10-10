"""延续教材 json → bytes → HTTP；这里只读取 DeepSeek 密钥。"""

import json
import socket
import queue
import threading
import urllib.error
import urllib.request

from .config import API_URL, KEY_FILE, MAX_OUTPUT_TOKENS, MODEL_NAME, TIMEOUT_SECONDS
from .privacy import redact


class ModelError(Exception):
    """只传安全的错误码，避免把供应商原文或请求头写入日志。"""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def parse_response(data):
    """先确认生成完整，再读取消息；截断的工具参数不会被执行。"""
    try:
        choice = data["choices"][0]
        finish = choice["finish_reason"]
        if finish not in {"stop", "tool_calls"}:
            raise ModelError("RESPONSE_TRUNCATED")
        raw = choice["message"]
        if raw.get("role") != "assistant":
            raise ModelError("BAD_RESPONSE")
        message = {"role": "assistant", "content": raw.get("content")}
        if message["content"] is not None and not isinstance(message["content"], str):
            raise ModelError("BAD_RESPONSE")
        if raw.get("tool_calls"):
            if not isinstance(raw["tool_calls"], list):
                raise ModelError("BAD_RESPONSE")
            message["tool_calls"] = raw["tool_calls"]
        if not message.get("content") and not message.get("tool_calls"):
            raise ModelError("EMPTY_RESPONSE")
        return {"message": message, "usage": data.get("usage", {})}
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ModelError("BAD_RESPONSE") from None


def ask_llm(messages, tools, *, model=MODEL_NAME, key_file=KEY_FILE, timeout_seconds=None):
    """一次调用，不重试；只返回模型消息与用量，不返回请求头或密钥。"""
    timeout = TIMEOUT_SECONDS if timeout_seconds is None else min(TIMEOUT_SECONDS, timeout_seconds)
    if timeout <= 0:
        raise ModelError("TURN_TIMEOUT")
    try:
        key = key_file.read_text(encoding="utf-8-sig").strip()
    except OSError:
        raise ModelError("KEY_UNAVAILABLE") from None
    if not key or any(char.isspace() for char in key):
        raise ModelError("KEY_INVALID")
    payload = {"model": model, "messages": messages, "tools": tools,
               "stream": False, "thinking": {"type": "disabled"},
               "max_tokens": MAX_OUTPUT_TOKENS}
    request = urllib.request.Request(
        API_URL, data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key}, method="POST")
    results = queue.Queue(maxsize=1)

    def request_once():
        # urllib 的 timeout 是网络阻塞时间，不保证整次请求总时间。
        # 守护线程只负责HTTP；即使结果晚到，也不能执行工具或改变账本。
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ModelError("RESPONSE_TOO_LARGE")
                data = json.loads(raw.decode("utf-8"))
            results.put((redact(parse_response(data), secrets=(key,)), None))
        except urllib.error.HTTPError as error:
            results.put((None, "HTTP_" + str(error.code)))
        except (TimeoutError, socket.timeout):
            results.put((None, "MODEL_TIMEOUT"))
        except urllib.error.URLError as error:
            code = "MODEL_TIMEOUT" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "NETWORK_ERROR"
            results.put((None, code))
        except ModelError as error:
            results.put((None, error.code))
        except Exception:
            results.put((None, "BAD_RESPONSE"))

    threading.Thread(target=request_once, daemon=True).start()
    try:
        result, error_code = results.get(timeout=timeout)
    except queue.Empty:
        # 本地停止等待不等于供应商取消计费；不自动重试这次请求。
        raise ModelError("MODEL_TIMEOUT") from None
    if error_code:
        raise ModelError(error_code)
    return result


def check_secret_leaks(paths, *, key_file=KEY_FILE):
    """交付验证用：只返回是否发现实际Key，不打印Key或命中内容。"""
    try:
        key = key_file.read_text(encoding="utf-8-sig").strip().encode("utf-8")
    except OSError:
        return {"verified": False, "reason": "key_unavailable"}
    if not key:
        return {"verified": False, "reason": "key_empty"}
    scanned = 0
    matches = 0
    for path in paths:
        if path.is_file() and path.resolve() != key_file.resolve():
            scanned += 1
            matches += int(key in path.read_bytes())
    return {"verified": True, "scanned_files": scanned, "matching_files": matches}
