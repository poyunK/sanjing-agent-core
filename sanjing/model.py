"""模型适配层：把服务商响应转换成 Agent 循环使用的统一消息结构。"""

import json
import queue
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request

from .config import (API_URL, ARK_API_URL, ARK_KEY_FILE, ARK_MAX_OUTPUT_TOKENS, KEY_FILE,
                     MAX_OUTPUT_TOKENS, MODEL_NAME, PROVIDER_NAME, TIMEOUT_SECONDS)
from .key_loader import KeyFileError, load_ark_key
from .privacy import redact

MAX_RESPONSE_BYTES = 2_000_000


class ModelError(Exception):
    """只携带安全错误码，避免把供应商原文或请求头写入日志。"""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def parse_response(data):
    """DeepSeek Chat Completions 解析器，保持旧协议调用兼容。"""
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


def _response_usage(raw_usage):
    """Responses usage 用 input/output 命名；同时保留供应商的细分统计。"""
    if raw_usage is None:
        return {}
    if not isinstance(raw_usage, dict):
        raise ModelError("BAD_RESPONSE")
    result = {}
    for source, target in (("input_tokens", "prompt_tokens"),
                           ("output_tokens", "completion_tokens"),
                           ("total_tokens", "total_tokens")):
        if source in raw_usage:
            value = raw_usage[source]
            if type(value) is not int or value < 0:
                raise ModelError("BAD_RESPONSE")
            result[target] = value
    for key in ("input_tokens", "output_tokens"):
        if key in raw_usage:
            result[key] = raw_usage[key]
    for key in ("input_tokens_details", "output_tokens_details"):
        details = raw_usage.get(key)
        if details is not None:
            if not isinstance(details, dict):
                raise ModelError("BAD_RESPONSE")
            result[key] = details
    return result


def parse_responses_response(data):
    """解析 Responses output；私有续轮数据仅保留重放所需字段。"""
    if not isinstance(data, dict):
        raise ModelError("BAD_RESPONSE")
    status = data.get("status")
    if status == "incomplete":
        raise ModelError("RESPONSE_TRUNCATED")
    if status == "failed":
        raise ModelError("RESPONSE_FAILED")
    if status != "completed":
        raise ModelError("BAD_RESPONSE")
    output = data.get("output")
    if not isinstance(output, list):
        raise ModelError("BAD_RESPONSE")

    text_parts = []
    calls = []
    replay_items = []
    for item in output:
        if not isinstance(item, dict):
            raise ModelError("BAD_RESPONSE")
        item_type = item.get("type")
        if item_type == "reasoning":
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise ModelError("BAD_RESPONSE")
            replay = {"id": item_id, "type": "reasoning"}
            encrypted = item.get("encrypted_content")
            if encrypted is not None:
                if not isinstance(encrypted, str):
                    raise ModelError("BAD_RESPONSE")
                replay["encrypted_content"] = encrypted
            # 不复制 summary、content 或其他可能含内部思维的字段。
            replay_items.append(replay)
            continue

        if item_type == "message":
            if item.get("role") != "assistant":
                raise ModelError("BAD_RESPONSE")
            item_status = item.get("status")
            if item_status == "incomplete":
                raise ModelError("RESPONSE_TRUNCATED")
            if item_status not in (None, "completed"):
                raise ModelError("BAD_RESPONSE")
            content = item.get("content")
            if not isinstance(content, list):
                raise ModelError("BAD_RESPONSE")
            replay_content = []
            for part in content:
                if not isinstance(part, dict):
                    raise ModelError("BAD_RESPONSE")
                if part.get("type") == "output_text":
                    text = part.get("text")
                    if not isinstance(text, str):
                        raise ModelError("BAD_RESPONSE")
                    text_parts.append(text)
                    replay_content.append({"type": "output_text", "text": text})
                elif part.get("type") == "refusal":
                    refusal = part.get("refusal")
                    if not isinstance(refusal, str):
                        raise ModelError("BAD_RESPONSE")
                    text_parts.append(refusal)
                    replay_content.append({"type": "refusal", "refusal": refusal})
                else:
                    raise ModelError("BAD_RESPONSE")
            replay = {"type": "message", "role": "assistant", "content": replay_content,
                      "status": "completed"}
            if isinstance(item.get("id"), str):
                replay["id"] = item["id"]
            replay_items.append(replay)
            continue

        if item_type == "function_call":
            item_status = item.get("status")
            if item_status == "incomplete":
                raise ModelError("RESPONSE_TRUNCATED")
            if item_status not in (None, "completed"):
                raise ModelError("BAD_RESPONSE")
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if (not isinstance(call_id, str) or not call_id
                    or not isinstance(name, str) or not name
                    or not isinstance(arguments, str)):
                raise ModelError("BAD_RESPONSE")
            calls.append({"id": call_id, "type": "function", "function": {
                "name": name, "arguments": arguments,
            }})
            replay = {"type": "function_call", "call_id": call_id, "name": name,
                      "arguments": arguments, "status": "completed"}
            if isinstance(item.get("id"), str):
                replay["id"] = item["id"]
            replay_items.append(replay)
            continue

        # 目前适配只接受消息、函数调用和加密 reasoning 项。
        raise ModelError("BAD_RESPONSE")

    content = "".join(text_parts) if text_parts else None
    if not content and not calls:
        raise ModelError("EMPTY_RESPONSE")
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    if replay_items:
        # run_turn 只在内存中继续使用它；store、trace 和页面都不读取此字段。
        message["_responses_output"] = replay_items
    return {"message": message, "usage": _response_usage(data.get("usage"))}


def _responses_input(messages):
    """把内核的 Chat 消息转换成 Responses input 项。"""
    if not isinstance(messages, list):
        raise ModelError("BAD_REQUEST")
    items = []
    for message in messages:
        if not isinstance(message, dict):
            raise ModelError("BAD_REQUEST")
        role = message.get("role")
        if role in {"system", "developer", "user"}:
            content = message.get("content")
            if not isinstance(content, str):
                raise ModelError("BAD_REQUEST")
            items.append({"role": role, "content": content})
        elif role == "assistant":
            previous_output = message.get("_responses_output")
            if previous_output is not None:
                if not isinstance(previous_output, list):
                    raise ModelError("BAD_REQUEST")
                items.extend(previous_output)
                continue
            content = message.get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ModelError("BAD_REQUEST")
                items.append({"role": "assistant", "content": content})
            for call in message.get("tool_calls", []):
                function = call.get("function") if isinstance(call, dict) else None
                if (not isinstance(call, dict) or not isinstance(call.get("id"), str)
                        or not isinstance(function, dict)
                        or not isinstance(function.get("name"), str)
                        or not isinstance(function.get("arguments"), str)):
                    raise ModelError("BAD_REQUEST")
                items.append({"type": "function_call", "call_id": call["id"],
                              "name": function["name"], "arguments": function["arguments"]})
        elif role == "tool":
            call_id = message.get("tool_call_id")
            content = message.get("content")
            if not isinstance(call_id, str) or not call_id or not isinstance(content, str):
                raise ModelError("BAD_REQUEST")
            items.append({"type": "function_call_output", "call_id": call_id,
                          "output": content})
        else:
            raise ModelError("BAD_REQUEST")
    return items


def _responses_tools(tools):
    """把 Chat Completions function 定义改为 Responses 的扁平结构。"""
    if not isinstance(tools, list):
        raise ModelError("BAD_REQUEST")
    result = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if (not isinstance(tool, dict) or tool.get("type") != "function"
                or not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not isinstance(function.get("parameters"), dict)):
            raise ModelError("BAD_REQUEST")
        response_tool = {"type": "function", "name": function["name"],
                         "parameters": function["parameters"], "strict": False}
        if isinstance(function.get("description"), str):
            response_tool["description"] = function["description"]
        result.append(response_tool)
    return result


def _same_origin(first, second):
    try:
        left = urllib.parse.urlsplit(first)
        right = urllib.parse.urlsplit(second)
        if right.username is not None or right.password is not None:
            return False
        left_port = left.port or (443 if left.scheme.lower() == "https" else 80)
        right_port = right.port or (443 if right.scheme.lower() == "https" else 80)
        return (left.scheme.lower(), (left.hostname or "").lower(), left_port) == (
            right.scheme.lower(), (right.hostname or "").lower(), right_port
        )
    except ValueError:
        return False


class _CrossOriginRedirect(Exception):
    pass


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """允许固定供应商 origin 内跳转，跨域时在携带 Authorization 前拒绝。"""

    def redirect_request(self, request, response, code, message, headers, new_url):
        absolute_url = urllib.parse.urljoin(request.full_url, new_url)
        if not _same_origin(ARK_API_URL, absolute_url):
            raise _CrossOriginRedirect()
        return super().redirect_request(request, response, code, message, headers, absolute_url)


def _ark_opener():
    return urllib.request.build_opener(_SameOriginRedirectHandler())


def _read_key(provider, key_file):
    path = key_file
    if path is None:
        path = ARK_KEY_FILE if provider == "ark" else KEY_FILE
    if provider == "ark":
        try:
            key = load_ark_key(path)
        except KeyFileError as error:
            raise ModelError(error.code) from None
    else:
        try:
            key = path.read_text(encoding="utf-8-sig").strip()
        except (OSError, TypeError, ValueError):
            raise ModelError("KEY_UNAVAILABLE") from None
        if not key or any(char.isspace() for char in key):
            raise ModelError("KEY_INVALID")
    return key


def _open_ark(request, timeout):
    return _ark_opener().open(request, timeout=timeout)


def _request_ark(request, timeout):
    return _open_ark(request, timeout)


def _request_deepseek(request, timeout):
    # 保留旧 DeepSeek 客户端路径，方便原有调用与离线替身继续工作。
    return urllib.request.urlopen(request, timeout=timeout)


def ask_llm(messages, tools, *, model=None, key_file=None, timeout_seconds=None, provider=None):
    """一次调用，不重试；provider 缺省时使用工程配置的默认供应商。"""
    selected_provider = provider or PROVIDER_NAME
    selected_provider = selected_provider.strip().lower() if isinstance(selected_provider, str) else ""
    if selected_provider not in {"ark", "deepseek"}:
        raise ModelError("PROVIDER_UNSUPPORTED")
    if model is None:
        if selected_provider == PROVIDER_NAME:
            selected_model = MODEL_NAME
        else:
            selected_model = "ark-code-latest" if selected_provider == "ark" else "deepseek-flash"
    else:
        selected_model = model
    if not isinstance(selected_model, str) or not selected_model.strip():
        raise ModelError("BAD_REQUEST")

    timeout = TIMEOUT_SECONDS if timeout_seconds is None else min(TIMEOUT_SECONDS, timeout_seconds)
    if timeout <= 0:
        raise ModelError("TURN_TIMEOUT")
    key = _read_key(selected_provider, key_file)
    if selected_provider == "ark":
        payload = {
            "model": selected_model,
            "input": _responses_input(messages),
            "tools": _responses_tools(tools),
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": min(ARK_MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS),
        }
        url = ARK_API_URL
    else:
        payload = {"model": selected_model, "messages": messages, "tools": tools,
                   "stream": False, "thinking": {"type": "disabled"},
                   "max_tokens": MAX_OUTPUT_TOKENS}
        url = API_URL
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST")
    results = queue.Queue(maxsize=1)

    def request_once():
        # urllib 的 timeout 是单次阻塞上限；守护线程再限制完整请求时间。
        try:
            if selected_provider == "ark":
                response_context = _request_ark(request, timeout)
            else:
                response_context = _request_deepseek(request, timeout)
            with response_context as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ModelError("RESPONSE_TOO_LARGE")
                data = json.loads(raw.decode("utf-8"))
            parsed = (parse_responses_response(data) if selected_provider == "ark"
                      else parse_response(data))
            results.put((redact(parsed, secrets=(key,)), None))
        except _CrossOriginRedirect:
            results.put((None, "CROSS_ORIGIN_REDIRECT"))
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


def check_secret_leaks(paths, *, key_file=None, provider="deepseek"):
    """交付验证默认扫描旧 DeepSeek Key；Ark 扫描必须显式指定 provider。"""
    try:
        if provider not in {"deepseek", "ark"}:
            return {"verified": False, "reason": "provider_unsupported"}
        key = _read_key(provider, key_file).encode("utf-8")
    except ModelError as error:
        reason = "key_unavailable" if error.code == "KEY_UNAVAILABLE" else "key_invalid"
        return {"verified": False, "reason": reason}
    scan_path = key_file or (ARK_KEY_FILE if provider == "ark" else KEY_FILE)
    scanned = 0
    matches = 0
    for path in paths:
        if path.is_file() and path.resolve() != scan_path.resolve():
            scanned += 1
            matches += int(key in path.read_bytes())
    return {"verified": True, "scanned_files": scanned, "matching_files": matches}
