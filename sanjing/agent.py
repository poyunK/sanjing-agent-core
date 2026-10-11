"""从教材第 9 章扩展：循环仍直白，安全和记账交给统一工具入口。"""

import json
import time
import queue
import threading
from pathlib import Path

from . import context, model, store
from .config import (DEFAULT_DB, MAX_STEPS, MAX_TOOL_CALLS_PER_STEP, MAX_USER_CHARS,
                     TURN_TIMEOUT_SECONDS, TIMEOUT_SECONDS, MAX_DATA_REQUESTS, DATA_TIMEOUT_SECONDS, MAX_SEARCH_REQUESTS)
from .privacy import redact

ERROR_TEXT = {
    "MODEL_TIMEOUT": "模型请求超时，本轮未完成；可以稍后重新提问。",
    "STEP_LIMIT": "已达到模型请求次数上限，本轮未完成。",
    "TURN_TIMEOUT": "已达到本轮总时间上限，已保存当前依据；本轮未完成。",
    "RESPONSE_TRUNCATED": "模型输出被截断，本轮未完成，未执行截断的工具请求。",
    "UNVERIFIED_OUTPUT": "模型未提交可校验的建议，本轮未完成；该段文字未作为建议发布。",
    "EMPTY_RESPONSE": "模型没有返回有效内容，本轮未完成。",
    "CONTEXT_LIMIT": "本轮上下文超过长度预算，本轮未完成。",
    "KEY_UNAVAILABLE": "无法读取指定的 Key 文件，本轮未完成。",
    "BAD_TOOL_PROTOCOL": "工具调用协议不完整，本轮未完成。",
    "INTERRUPTED": "本轮执行占用已结束，已保留账目；旧执行不能继续发布建议。",
}


def validate_tool_calls(calls, seen_ids):
    """先校验整个批次，保证所有调用都有可配对的 ID；只校协议，不代替工具权限检查。"""
    if not isinstance(calls, list) or len(calls) > MAX_TOOL_CALLS_PER_STEP:
        raise model.ModelError("BAD_TOOL_PROTOCOL")
    batch_ids = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise model.ModelError("BAD_TOOL_PROTOCOL")
        call_id = call.get("id")
        function = call.get("function")
        if (not isinstance(call_id, str) or not call_id or len(call_id) > 200
                or call_id in seen_ids or call_id in batch_ids
                or not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or len(function["name"]) > 100
                or not isinstance(function.get("arguments"), str)):
            raise model.ModelError("BAD_TOOL_PROTOCOL")
        batch_ids.add(call_id)
    seen_ids.update(batch_ids)


def render_recommendations(records):
    """回复从已校验账目渲染；数字直接来自程序计算，模型不能在最后换掉它。"""
    lines = []
    for record in records:
        data = record["data"]
        if data.get("kind") == "career_decision":
            lines.extend(["", "【" + data["title"] + "】", "比较选项：" + "；".join(data["options"])])
            for claim in data["claims"]:
                label = {"fact": "来源字段（程序填入）", "user_statement": "用户自述",
                         "inference": "分析（模型生成）"}[claim["kind"]]
                lines.append(label + "：" + claim["text"])
                for source in claim["resolved"]:
                    lines.append("  " + source["evidence_id"] + " " + source["field_path"]
                                 + " = " + json.dumps(source["value"], ensure_ascii=False))
            lines.extend(["待核实：" + "；".join(data["unknowns"]),
                          "限制：" + "；".join(data["limitations"]),
                          "下一步：" + "；".join(data["next_actions"]), "建议 ID：" + record["id"]])
            continue
        lines.append("以下为演示规则下的职业探索建议，尚未验证职业推荐效度。")
        lines.extend(["", "【" + data.get("title", "探索建议") + "】",
                      "说明（模型生成）：" + data.get("explanation", ""),
                      "计算记录：" + json.dumps(data.get("computed", {}), ensure_ascii=False),
                      "规则版本：" + data.get("rule_version", "未注明"),
                      "限制：" + "；".join(data.get("limitations", [])),
                      "下一步：" + data.get("next_action", "先验证缺失条件"),
                      "建议 ID：" + record["id"]])
    return redact("\n".join(lines))


def _request_model(client, messages, definitions, deadline):
    """等待纯请求函数，迟到的模型返回不能继续执行工具；默认客户端也有自身网络超时。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise model.ModelError("TURN_TIMEOUT")
    timeout = min(TIMEOUT_SECONDS, remaining)
    answers = queue.Queue(maxsize=1)

    def request():
        try:
            if client is None:
                result = model.ask_llm(messages, definitions, timeout_seconds=timeout)
            else:
                result = client(messages, definitions)
            answers.put((result, None))
        except Exception as error:
            answers.put((None, error))

    threading.Thread(target=request, daemon=True).start()
    try:
        result, error = answers.get(timeout=timeout)
    except queue.Empty:
        code = "TURN_TIMEOUT" if time.monotonic() >= deadline else "MODEL_TIMEOUT"
        raise model.ModelError(code) from None
    if error:
        raise error
    if time.monotonic() >= deadline:
        raise model.ModelError("TURN_TIMEOUT")
    return result


def run_turn(session_id, user_text, *, db_path=None, client=None, data_client=None, search_client=None,
             trace=False, on_progress=None, request_id=None):
    """先领取会话，再执行一轮。网页重发同一操作时传相同 request_id，避免重复收费。

    API 无状态并不意味着工程无状态：SQLite 保存请求结果和短事务租约。
    不传 request_id 时仍串行保护会话，但相同文字被视为一次新的用户操作。
    """
    db_path = Path(db_path or DEFAULT_DB)
    if not isinstance(user_text, str) or not user_text.strip() or len(user_text) > MAX_USER_CHARS:
        raise ValueError("问题必须非空且不超过4000个字符")
    if request_id is not None and (not isinstance(request_id, str) or not request_id
                                   or len(request_id) > 128 or not all(c.isalnum() or c in '-_.' for c in request_id)):
        raise ValueError("request_id 必须是最多128字符的字母、数字、短横线、下划线或点")
    session_id = store.ensure_session(db_path, session_id)
    claim = store.begin_request(db_path, session_id, user_text.strip(), request_id,
                                TURN_TIMEOUT_SECONDS + TIMEOUT_SECONDS + 30)
    if claim['state'] == 'cached':
        return dict(claim['result'], replayed=True)
    if claim['state'] != 'owned':
        code = {'busy': 'SESSION_BUSY', 'conflict': 'REQUEST_ID_CONFLICT',
                'interrupted': 'INTERRUPTED'}[claim['state']]
        return {'session_id': session_id, 'turn_id': claim.get('turn_id'), 'status': 'incomplete',
                'answer': {'SESSION_BUSY': '本会话上一轮仍在执行，请等待；没有发出新请求。',
                           'REQUEST_ID_CONFLICT': '同一请求ID不能用于不同问题；没有发出新请求。',
                           'INTERRUPTED': '该请求之前已中断，没有自动重试收费服务；新操作请使用新请求ID。'}[code],
                'error_code': code, 'ledger_path': None, 'usage': {'requests': 0},
                'data_usage': {'requests': 0}, 'search_usage': {'requests': 0}, 'replayed': False}
    turn_id = claim['turn_id']
    try:
        result = _run_owned_turn(session_id, user_text, db_path=db_path, client=client,
                                 data_client=data_client, search_client=search_client, trace=trace, on_progress=on_progress,
                                 turn_id=turn_id)
        store.complete_request(db_path, session_id, turn_id, result)
        return result
    finally:
        # 包括查看页失败：租约也能释放；已保存的证据不回滚、不重复收费。
        turn = store.get_turn(db_path, session_id, turn_id)
        if turn and turn['status'] == 'running':
            store.finish_turn(db_path, session_id, turn_id, 'incomplete', '本轮中断，已保存当前依据。', 'INTERRUPTED')
        store.complete_request(db_path, session_id, turn_id)


def _run_owned_turn(session_id, user_text, *, db_path, turn_id, client=None, data_client=None, search_client=None,
                    trace=False, on_progress=None):
    """网站未来可调用此函数。client 可替换为离线模拟；不必把 Key 传入工具。"""
    from .tools import execute_tool, tool_definitions
    from .viewer import write_snapshot

    db_path = Path(db_path or DEFAULT_DB)
    session_id = store.ensure_session(db_path, session_id)
    if not isinstance(user_text, str) or not user_text.strip() or len(user_text) > MAX_USER_CHARS:
        raise ValueError("问题必须非空且不超过4000个字符")
    user_text = redact(user_text.strip())
    message_id = store.add_message(db_path, session_id, turn_id, "user", user_text)
    started = time.monotonic()
    deadline = started + TURN_TIMEOUT_SECONDS
    if data_client is None:
        from .datapro import query_data
        data_client = lambda query: query_data(query, timeout_seconds=max(0.001, min(DATA_TIMEOUT_SECONDS, deadline - time.monotonic())))
    data_usage = {"requests": 0, "success": 0, "failed": 0, "timeouts": 0, "cache_hits": 0, "blocked": 0}
    search_usage = dict(data_usage)
    ctx = {"db_path": db_path, "session_id": session_id, "turn_id": turn_id,
           "user_message_id": message_id, "allowed_evidence_ids": set(),
           "pending_recommendations": [], "data_client": data_client, "deadline": deadline,
           "data_usage": data_usage, "data_cache": {},
           "search_client": search_client, "search_usage": search_usage, "search_cache": {}}
    user_evidence_id = store.add_evidence(db_path, session_id, turn_id, "user_input",
                                         {"message_id": message_id, "quote": user_text, "source": "当前用户原话"})
    ctx["allowed_evidence_ids"].add(user_evidence_id)
    usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
             "unreported_requests": 0, "cached_input_tokens": 0, "reasoning_tokens": 0,
             "reported_details_requests": 0}
    status, answer, error_code = "incomplete", "", None

    def progress(stage):
        event = {"stage": redact(stage), "elapsed_seconds": round(time.monotonic() - started, 2)}
        if on_progress:
            on_progress(event)

    try:
        try:
            messages = context.build_messages(db_path, session_id, turn_id, message_id)
        except ValueError as error:
            if str(error) == "CONTEXT_LIMIT":
                raise model.ModelError("CONTEXT_LIMIT") from None
            raise
        seen_ids = set()
        for step in range(MAX_STEPS):
            if not store.owns_lease(db_path, session_id, turn_id):
                raise model.ModelError('INTERRUPTED')
            if time.monotonic() >= deadline:
                raise model.ModelError("TURN_TIMEOUT")
            if context.char_size(messages) > context.CONTEXT_CHAR_BUDGET:
                raise model.ModelError("CONTEXT_LIMIT")
            progress(f"请求模型 {step + 1}/{MAX_STEPS}（单次最长60秒）")
            if trace:
                progress("消息结构 " + ", ".join(item["role"] for item in messages))
            usage["requests"] += 1
            try:
                reply = _request_model(client, messages, tool_definitions(), deadline)
            except BaseException:
                usage['unreported_requests'] += 1
                raise
            if not isinstance(reply, dict) or not isinstance(reply.get("message"), dict):
                usage['unreported_requests'] += 1
                raise model.ModelError("BAD_RESPONSE")
            reply_usage = reply.get("usage")
            if not isinstance(reply_usage, dict):
                reply_usage = {}
            if not all(type(reply_usage.get(key)) is int and reply_usage[key] >= 0
                       for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
                usage["unreported_requests"] += 1
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = reply_usage.get(key, 0)
                if type(value) is int and value >= 0:
                    usage[key] += value
            # 只累计已报告的固定数字字段，不把供应商任意附加内容写进账本。
            details_complete = True
            for container_name, field, target in (
                    ('input_tokens_details', 'cached_tokens', 'cached_input_tokens'),
                    ('output_tokens_details', 'reasoning_tokens', 'reasoning_tokens')):
                details = reply_usage.get(container_name)
                value = details.get(field) if isinstance(details, dict) else None
                if type(value) is int and value >= 0:
                    usage[target] += value
                else:
                    details_complete = False
            if details_complete:
                usage['reported_details_requests'] += 1
            message = reply["message"]
            if message.get("role") != "assistant":
                raise model.ModelError("BAD_RESPONSE")
            calls = message.get("tool_calls")

            # 重要：有文字不代表结束，先处理工具调用。
            if calls:
                validate_tool_calls(calls, seen_ids)
                messages.append(message)
                submitted = False
                clarification = None
                batch_ok = True
                for call in calls:
                    if not store.owns_lease(db_path, session_id, turn_id):
                        raise model.ModelError('INTERRUPTED')
                    if time.monotonic() >= deadline:
                        raise model.ModelError("TURN_TIMEOUT")
                    name = call["function"]["name"]
                    progress("执行工具：" + name)
                    result = execute_tool(ctx, call)
                    if not store.owns_lease(db_path, session_id, turn_id):
                        raise model.ModelError('INTERRUPTED')
                    batch_ok = batch_ok and bool(result.get("ok"))
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": json.dumps(redact(result), ensure_ascii=False, separators=(",", ":"))})
                    progress("记录依据：" + ("成功" if result.get("ok") else "拒绝或失败"))
                    if name == "search_web":
                        progress("豆包搜索请求 " + str(search_usage["requests"]) + "/" + str(MAX_SEARCH_REQUESTS)
                                 + "，成功 " + str(search_usage["success"]) + "，失败 " + str(search_usage["failed"]))
                    if name == "query_professional_data":
                        progress("专业数据请求 " + str(data_usage["requests"]) + "/" + str(MAX_DATA_REQUESTS) + "，成功 "
                                 + str(data_usage["success"]) + "，失败 " + str(data_usage["failed"]))
                    if trace:
                        progress("配对 " + call["id"] + " → " + ", ".join(result.get("evidence_ids", [])))
                    if time.monotonic() >= deadline:
                        raise model.ModelError("TURN_TIMEOUT")
                    if name in {"submit_recommendation", "submit_decision"} and result.get("ok"):
                        submitted = True
                    if name == "ask_user" and result.get("ok"):
                        clarification = result["data"]["questions"]
                if submitted and batch_ok:
                    progress("校验回复：读取已提交的建议")
                    records = store.list_recommendations(db_path, session_id, turn_id)
                    if not records:
                        raise model.ModelError("SUBMIT_FAILED")
                    answer = render_recommendations(records)
                    status = "completed"
                    break
                if clarification:
                    answer = "需要补充条件：\n" + "\n".join("- " + question for question in clarification)
                    status = "needs_input"
                    break
                continue

            # 普通回答没有 evidence 约束，不发布为已校验建议。
            text = message.get("content")
            if not isinstance(text, str) or not text.strip():
                raise model.ModelError("EMPTY_RESPONSE")
            raise model.ModelError("UNVERIFIED_OUTPUT")
        else:
            raise model.ModelError("STEP_LIMIT")
    except model.ModelError as error:
        error_code = error.code
        answer = ERROR_TEXT.get(error_code, "本轮未完成（" + error_code + "），请检查配置或稍后重试。")
    except KeyboardInterrupt:
        error_code = "CANCELLED"
        answer = "本轮已由你中断，未完成；已保存当前依据与执行事件。"
    except Exception:
        # 不打印原异常：它可能带工具资料、供应商原文或本地路径。
        error_code = "INTERNAL_ERROR"
        answer = "本轮遇到内部错误，未完成；请查看测试或执行事件定位。"
    usage["data_requests"] = dict(data_usage)
    usage["search_requests"] = dict(search_usage)
    published = store.finish_turn(db_path, session_id, turn_id, status, answer, error_code, usage, require_lease=True)
    if published:
        store.add_message(db_path, session_id, turn_id, "assistant", answer)
    else:
        status, error_code = 'incomplete', 'INTERRUPTED'
        answer = ERROR_TEXT[error_code]
    progress("生成账本查看页")
    output_dir = db_path.parent / "ledger"
    ledger_path = write_snapshot(db_path, session_id, turn_id, output_dir)
    return {"session_id": session_id, "turn_id": turn_id, "status": status,
            "answer": answer, "error_code": error_code, "ledger_path": str(ledger_path),
            "usage": usage, "data_usage": dict(data_usage), "search_usage": dict(search_usage)}
