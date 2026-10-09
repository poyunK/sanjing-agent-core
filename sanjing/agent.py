"""从教材第 9 章扩展：循环仍直白，安全和记账交给统一工具入口。"""

import json
import time
from pathlib import Path

from . import context, model, store
from .config import DEFAULT_DB, MAX_STEPS, MAX_TOOL_CALLS_PER_STEP, MAX_USER_CHARS
from .privacy import redact

ERROR_TEXT = {
    "MODEL_TIMEOUT": "模型请求超时，本轮未完成；可以稍后重新提问。",
    "STEP_LIMIT": "已达到六次模型请求上限，本轮未完成。",
    "RESPONSE_TRUNCATED": "模型输出被截断，本轮未完成，未执行截断的工具请求。",
    "UNVERIFIED_OUTPUT": "模型未提交可校验的建议，本轮未完成；该段文字未作为建议发布。",
    "EMPTY_RESPONSE": "模型没有返回有效内容，本轮未完成。",
    "CONTEXT_LIMIT": "本轮上下文超过长度预算，本轮未完成。",
    "KEY_UNAVAILABLE": "无法读取指定的 Key 文件，本轮未完成。",
    "BAD_TOOL_PROTOCOL": "工具调用协议不完整，本轮未完成。",
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
    lines = ["以下为演示规则下的职业探索建议，尚未验证职业推荐效度。"]
    for record in records:
        data = record["data"]
        lines.extend(["", "【" + data.get("title", "探索建议") + "】",
                      "说明（模型生成）：" + data.get("explanation", ""),
                      "计算记录：" + json.dumps(data.get("computed", {}), ensure_ascii=False),
                      "规则版本：" + data.get("rule_version", "未注明"),
                      "限制：" + "；".join(data.get("limitations", [])),
                      "下一步：" + data.get("next_action", "先验证缺失条件"),
                      "建议 ID：" + record["id"]])
    return redact("\n".join(lines))


def run_turn(session_id, user_text, *, db_path=None, client=None, trace=False, on_progress=None):
    """网站未来可调用此函数。client 可替换为离线模拟；不必把 Key 传入工具。"""
    from .tools import execute_tool, tool_definitions
    from .viewer import write_snapshot

    db_path = Path(db_path or DEFAULT_DB)
    session_id = store.ensure_session(db_path, session_id)
    if not isinstance(user_text, str) or not user_text.strip() or len(user_text) > MAX_USER_CHARS:
        raise ValueError("问题必须非空且不超过4000个字符")
    user_text = redact(user_text.strip())
    turn_id = store.start_turn(db_path, session_id)
    message_id = store.add_message(db_path, session_id, turn_id, "user", user_text)
    ctx = {"db_path": db_path, "session_id": session_id, "turn_id": turn_id,
           "user_message_id": message_id, "allowed_evidence_ids": set(),
           "pending_recommendations": []}
    user_evidence_id = store.add_evidence(db_path, session_id, turn_id, "user_input",
                                         {"message_id": message_id, "quote": user_text, "source": "当前用户原话"})
    ctx["allowed_evidence_ids"].add(user_evidence_id)
    client = client or model.ask_llm
    started = time.monotonic()
    usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    status, answer, error_code = "incomplete", "", None

    def progress(stage):
        event = {"stage": redact(stage), "elapsed_seconds": round(time.monotonic() - started, 2)}
        if on_progress:
            on_progress(event)

    try:
        messages = context.build_messages(db_path, session_id, turn_id, message_id)
        seen_ids = set()
        for step in range(MAX_STEPS):
            if context.char_size(messages) > context.CONTEXT_CHAR_BUDGET:
                raise model.ModelError("CONTEXT_LIMIT")
            progress(f"请求模型 {step + 1}/{MAX_STEPS}（单次最长60秒）")
            if trace:
                progress("消息结构 " + ", ".join(item["role"] for item in messages))
            usage["requests"] += 1
            reply = client(messages, tool_definitions())
            if not isinstance(reply, dict) or not isinstance(reply.get("message"), dict):
                raise model.ModelError("BAD_RESPONSE")
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = reply.get("usage", {}).get(key, 0)
                if type(value) is int and value >= 0:
                    usage[key] += value
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
                    name = call["function"]["name"]
                    progress("执行工具：" + name)
                    result = execute_tool(ctx, call)
                    batch_ok = batch_ok and bool(result.get("ok"))
                    messages.append({"role": "tool", "tool_call_id": call["id"],
                                     "content": json.dumps(redact(result), ensure_ascii=False)})
                    progress("记录依据：" + ("成功" if result.get("ok") else "拒绝或失败"))
                    if trace:
                        progress("配对 " + call["id"] + " → " + ", ".join(result.get("evidence_ids", [])))
                    if name == "submit_recommendation" and result.get("ok"):
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
    store.finish_turn(db_path, session_id, turn_id, status, answer, error_code, usage)
    store.add_message(db_path, session_id, turn_id, "assistant", answer)
    progress("生成账本查看页")
    output_dir = db_path.parent / "ledger"
    ledger_path = write_snapshot(db_path, session_id, turn_id, output_dir)
    return {"session_id": session_id, "turn_id": turn_id, "status": status,
            "answer": answer, "error_code": error_code, "ledger_path": str(ledger_path), "usage": usage}
