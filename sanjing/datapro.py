"""固定端点的 DataPro Streamable HTTP MCP 客户端。

此模块独占 DataPro Key 的读取；远端内容按不可信数据处理，不能执行其中的指令。
"""

import json
import math
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import privacy


DATAPRO_URL = "https://datapro.hqd.cn-beijing.volces.com/mcp"
PROTOCOL_VERSION = "2025-03-26"
MAX_RESPONSE_BYTES = 1_048_576
MAX_TIMEOUT_SECONDS = 60.0
DEFAULT_KEY_FILE = Path("C:/Users/pla53519/Documents/火山keyUTF8.txt")
KEY_ENV_NAME = "SANJING_DATAPRO_KEY_FILE"
_MASK = "[密钥已隐藏]"


class DataproError(Exception):
    """只携带安全错误码，不包含密钥、响应正文或异常原文。"""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _CrossOriginRedirect(Exception):
    pass


def _key_path(key_file=None):
    if key_file is not None:
        return Path(key_file)
    configured = os.environ.get(KEY_ENV_NAME)
    return Path(configured) if configured else DEFAULT_KEY_FILE


def _load_key(key_file=None):
    """读取唯一授权来源的 DataPro Key；调用者不得记录返回值。"""
    try:
        key = _key_path(key_file).read_text(encoding="utf-8-sig").strip()
    except (OSError, TypeError, ValueError):
        raise DataproError("KEY_UNAVAILABLE") from None
    if not key or any(character.isspace() for character in key):
        raise DataproError("KEY_INVALID")
    return key


def _same_origin(first, second):
    left = urllib.parse.urlsplit(first)
    right = urllib.parse.urlsplit(second)
    left_port = left.port or (443 if left.scheme.lower() == "https" else 80)
    right_port = right.port or (443 if right.scheme.lower() == "https" else 80)
    return (left.scheme.lower(), (left.hostname or "").lower(), left_port) == (
        right.scheme.lower(), (right.hostname or "").lower(), right_port
    )


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """只允许固定供应商 origin 内的 HTTP 重定向。"""

    def redirect_request(self, request, response, code, message, headers, new_url):
        absolute_url = urllib.parse.urljoin(request.full_url, new_url)
        if not _same_origin(DATAPRO_URL, absolute_url):
            raise _CrossOriginRedirect()
        return super().redirect_request(request, response, code, message, headers, absolute_url)


def _opener():
    return urllib.request.build_opener(_SameOriginRedirectHandler())


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DataproError("TIMEOUT")
    return remaining


def _safe_session_id(headers):
    value = _header_value(headers, "Mcp-Session-Id")
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 256 or "\r" in value or "\n" in value:
        raise DataproError("BAD_SESSION")
    return value


def _header_value(headers, name):
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except (AttributeError, TypeError):
        value = None
    if value is not None:
        return value
    try:
        for key, candidate in headers.items():
            if str(key).lower() == name.lower():
                return candidate
    except (AttributeError, TypeError):
        pass
    return None


class _McpClient:
    def __init__(self, key, deadline):
        self._key = key
        self._deadline = deadline
        self._session_id = None
        self._next_id = 1

    def _post(self, message, *, require_rpc_response=True):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "X-Agent-Plan-Key": self._key,
        }
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            body = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
            request = urllib.request.Request(DATAPRO_URL, data=body, headers=headers, method="POST")
            with _opener().open(request, timeout=_remaining(self._deadline)) as response:
                status = getattr(response, "status", 200)
                if not isinstance(status, int) or not 200 <= status < 300:
                    raise DataproError("HTTP_ERROR")
                response_headers = getattr(response, "headers", None)
                session_id = _safe_session_id(response_headers)
                content_type = _header_value(response_headers, "Content-Type") or ""
                if require_rpc_response and content_type.split(";", 1)[0].strip().lower() == "text/event-stream":
                    raw_response = _read_sse_stream(response, message.get("id"), self._deadline)
                else:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise DataproError("RESPONSE_TOO_LARGE")
                    if not require_rpc_response:
                        raw_response = None
                    else:
                        raw_response = _decode_rpc_response(raw, content_type, message.get("id"))
        except DataproError:
            raise
        except _CrossOriginRedirect:
            raise DataproError("CROSS_ORIGIN_REDIRECT") from None
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                raise DataproError("REDIRECT_REJECTED") from None
            raise DataproError("HTTP_" + str(error.code)) from None
        except (TimeoutError, socket.timeout):
            raise DataproError("TIMEOUT") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise DataproError("TIMEOUT") from None
            raise DataproError("NETWORK_ERROR") from None
        except Exception:
            raise DataproError("NETWORK_ERROR") from None

        if session_id is not None:
            self._session_id = session_id
        if not require_rpc_response:
            return None
        return raw_response

    def _rpc(self, method, params):
        request_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        response = self._post(message)
        if "error" in response:
            raise DataproError("MCP_REMOTE_ERROR")
        result = response.get("result")
        if not isinstance(result, dict):
            raise DataproError("BAD_RESPONSE")
        return result

    def query(self, query):
        initialized = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "SanJAgent", "version": "1.0"},
        })
        if initialized.get("protocolVersion") != PROTOCOL_VERSION:
            raise DataproError("PROTOCOL_MISMATCH")

        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, require_rpc_response=False)

        listed = self._rpc("tools/list", {})
        tools = listed.get("tools")
        if not isinstance(tools, list) or len(tools) != 1 or listed.get("nextCursor"):
            raise DataproError("TOOL_SCHEMA_MISMATCH")
        tool = tools[0]
        if not isinstance(tool, dict) or tool.get("name") != "dataPro_search":
            raise DataproError("TOOL_SCHEMA_MISMATCH")
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise DataproError("TOOL_SCHEMA_MISMATCH")
        properties = schema.get("properties")
        required = schema.get("required")
        allowed_schema_fields = {"type", "properties", "required", "additionalProperties",
                                 "$schema", "title", "description"}
        if (set(schema) - allowed_schema_fields
                or schema.get("additionalProperties", False) is not False
                or not isinstance(properties, dict) or set(properties) != {"query"}
                or not isinstance(properties.get("query"), dict)
                or properties["query"].get("type") != "string"
                or not isinstance(required, list) or required != ["query"]):
            raise DataproError("TOOL_SCHEMA_MISMATCH")

        called = self._rpc("tools/call", {
            "name": "dataPro_search",
            "arguments": {"query": query},
        })
        if "isError" in called and not isinstance(called["isError"], bool):
            raise DataproError("BAD_RESPONSE")
        if called.get("isError") is True:
            raise DataproError("TOOL_CALL_ERROR")
        return _business_payload(called, self._key)


def _validate_rpc_envelope(value, request_id):
    if (not isinstance(value, dict) or value.get("jsonrpc") != "2.0"
            or type(value.get("id")) is not type(request_id)
            or value.get("id") != request_id):
        raise DataproError("BAD_RESPONSE")
    if "error" in value and not isinstance(value["error"], dict):
        raise DataproError("BAD_RESPONSE")
    return value


def _decode_rpc_response(raw, content_type, request_id):
    if not isinstance(raw, bytes):
        raise DataproError("BAD_RESPONSE")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "application/json":
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DataproError("BAD_RESPONSE") from None
        return _validate_rpc_envelope(value, request_id)
    if media_type == "text/event-stream":
        return _read_sse_response(raw, request_id)
    raise DataproError("BAD_CONTENT_TYPE")


class _SSEEventParser:
    def __init__(self, request_id):
        self.request_id = request_id
        self.data_lines = []

    def feed_line(self, line):
        if line == "":
            return self._finish_event()
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "data":
            self.data_lines.append(value)
        return None

    def finish(self):
        return self._finish_event()

    def _finish_event(self):
        if not self.data_lines:
            return None
        event_text = "\n".join(self.data_lines)
        self.data_lines.clear()
        try:
            event_value = json.loads(event_text)
        except json.JSONDecodeError:
            raise DataproError("BAD_RESPONSE") from None
        if not isinstance(event_value, dict) or event_value.get("jsonrpc") != "2.0":
            raise DataproError("BAD_RESPONSE")
        # Server notifications and unrelated responses may arrive before the call response.
        if "id" not in event_value:
            return None
        if (type(event_value.get("id")) is not type(self.request_id)
                or event_value.get("id") != self.request_id):
            return None
        return _validate_rpc_envelope(event_value, self.request_id)


def _read_sse_response(raw, request_id):
    """完整字节串的 SSE 解析器，供离线解析测试和调用复用。"""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise DataproError("BAD_RESPONSE") from None
    parser = _SSEEventParser(request_id)
    for line in text.splitlines():
        matched = parser.feed_line(line)
        if matched is not None:
            return matched
    matched = parser.finish()
    if matched is None:
        raise DataproError("MISSING_RPC_RESPONSE")
    return matched


def _read_sse_stream(response, request_id, deadline):
    """逐行读取 SSE，只在当前 RPC 响应到达后停止，不等待连接 EOF。"""
    parser = _SSEEventParser(request_id)
    total_bytes = 0
    line_buffer = bytearray()
    while True:
        _remaining(deadline)
        remaining_bytes = MAX_RESPONSE_BYTES - total_bytes
        try:
            chunk = response.readline(remaining_bytes + 1)
        except (TimeoutError, socket.timeout):
            raise DataproError("TIMEOUT") from None
        except Exception:
            raise DataproError("NETWORK_ERROR") from None
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise DataproError("BAD_RESPONSE")
        total_bytes += len(chunk)
        if total_bytes > MAX_RESPONSE_BYTES:
            raise DataproError("RESPONSE_TOO_LARGE")
        line_buffer.extend(chunk)
        while b"\n" in line_buffer:
            raw_line, _, rest = line_buffer.partition(b"\n")
            line_buffer = bytearray(rest)
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                raise DataproError("BAD_RESPONSE") from None
            matched = parser.feed_line(line)
            if matched is not None:
                return matched
    if line_buffer:
        try:
            line = bytes(line_buffer).decode("utf-8")
        except UnicodeDecodeError:
            raise DataproError("BAD_RESPONSE") from None
        matched = parser.feed_line(line)
        if matched is not None:
            return matched
    matched = parser.finish()
    if matched is None:
        raise DataproError("MISSING_RPC_RESPONSE")
    return matched


def _replace_exact(value, secret):
    if isinstance(value, str):
        return value.replace(secret, _MASK)
    if isinstance(value, list):
        return [_replace_exact(item, secret) for item in value]
    if isinstance(value, dict):
        replaced = {}
        for key, item in value.items():
            safe_key = _replace_exact(key, secret) if isinstance(key, str) else key
            replaced[safe_key] = _replace_exact(item, secret)
        return replaced
    return value


def _business_payload(tool_result, secret):
    if "structuredContent" in tool_result:
        payload = tool_result["structuredContent"]
        if not isinstance(payload, dict):
            raise DataproError("INVALID_BUSINESS_PAYLOAD")
    else:
        content = tool_result.get("content")
        if not isinstance(content, list):
            raise DataproError("INVALID_BUSINESS_PAYLOAD")
        text_parts = [item["text"] for item in content
                      if isinstance(item, dict) and item.get("type") == "text"
                      and isinstance(item.get("text"), str)]
        if not text_parts:
            raise DataproError("INVALID_BUSINESS_PAYLOAD")
        try:
            payload = json.loads("\n".join(text_parts))
        except json.JSONDecodeError:
            raise DataproError("INVALID_BUSINESS_PAYLOAD") from None
        if not isinstance(payload, dict):
            raise DataproError("INVALID_BUSINESS_PAYLOAD")
    return privacy.redact(_replace_exact(payload, secret))


def _query_once(query, timeout_seconds, key_file):
    deadline = time.monotonic() + timeout_seconds
    key = _load_key(key_file)
    return _McpClient(key, deadline).query(query)


def query_data(query, *, timeout_seconds=60, key_file=None):
    """通过固定 MCP 工具查询 DataPro，返回供应商业务 payload 字典。

    调用有总时限，最多 60 秒；失败时只抛出携带安全错误码的 DataproError。
    此函数不会重试，也不会将远端返回内容保存到本地。
    """
    if not isinstance(query, str) or not query.strip():
        raise DataproError("INVALID_QUERY")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0 or timeout_seconds > MAX_TIMEOUT_SECONDS
            or not math.isfinite(timeout_seconds)):
        raise DataproError("INVALID_TIMEOUT")

    results = queue.Queue(maxsize=1)

    def run():
        try:
            results.put_nowait((_query_once(query, float(timeout_seconds), key_file), None))
        except DataproError as error:
            results.put_nowait((None, error.code))
        except Exception:
            results.put_nowait((None, "BAD_RESPONSE"))

    # urllib 的 timeout 约束单次阻塞；守护线程再约束完整初始化、发现与调用流程。
    threading.Thread(target=run, name="datapro-query", daemon=True).start()
    try:
        result, error_code = results.get(timeout=float(timeout_seconds))
    except queue.Empty:
        raise DataproError("TIMEOUT") from None
    if error_code:
        raise DataproError(error_code)
    return result


def scan_key_leaks(paths, key_file=None):
    """扫描指定文件中实际 Key 的精确出现次数，只返回计数，不返回命中内容。"""
    key = _load_key(key_file).encode("utf-8")
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    try:
        key_path = _key_path(key_file).resolve()
        count = 0
        for item in paths:
            path = Path(item)
            if not path.is_file() or path.resolve() == key_path:
                continue
            count += path.read_bytes().count(key)
        return count
    except (OSError, TypeError, ValueError):
        raise DataproError("SCAN_FAILED") from None
