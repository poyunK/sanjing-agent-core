"""确定性模拟模型：展示同一主循环的数据流，不读取Key、不调用网络。"""

import json


def _reply(calls):
    """每次给新ID；这里只模拟选择工具，真实执行仍走 tools.execute_tool。"""
    from .store import new_id
    tool_calls = []
    for name, arguments in calls:
        tool_calls.append({"id": new_id("call"), "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}})
    return {"message": {"role": "assistant", "content": "离线模拟：正在请求工具。",
                        "tool_calls": tool_calls}, "usage": {}}


def ask_offline(messages, tools):
    """从本轮 tool 消息读取结果，再决定下一步，适合断点与打印观察。"""
    user_index = max(index for index, msg in enumerate(messages) if msg["role"] == "user")
    user_text = messages[user_index]["content"]
    names = {}
    results = []
    for msg in messages[user_index + 1:]:
        if msg["role"] == "assistant":
            for call in msg.get("tool_calls", []):
                names[call["id"]] = call["function"]["name"]
        elif msg["role"] == "tool":
            result = json.loads(msg["content"])
            results.append((names.get(msg["tool_call_id"]), result))

    # 用户可以主动演示拦截；这个危险工具根本没有注册或实现。
    if any(word in user_text.lower() for word in ("危险", "run_shell", "读取密钥", "删除文件")):
        if not results:
            return _reply([("run_shell", {"command": "blocked-demo"})])
        return _reply([("ask_user", {"fields": ["goal"]})])

    successful = [(name, result) for name, result in results if result.get("ok")]
    assessments = [result for name, result in successful if name == "get_assessment"]
    searches = [result for name, result in successful if name == "search_jobs"]
    calculations = [result["data"] for name, result in successful if name == "match_job"]
    if not assessments or not searches:
        if results:  # 出错时不在同一轮无限重复。
            return _reply([("ask_user", {"fields": ["goal", "experience"]})])
        return _reply([("get_assessment", {}),
                       ("search_jobs", {"query": "用户研究 产品经理", "limit": 2})])

    jobs = searches[-1]["data"]["results"]
    if not jobs:
        return _reply([("ask_user", {"fields": ["goal"]})])
    if not calculations:
        assessment_id = assessments[-1]["evidence_ids"][0]
        return _reply([("match_job", {
            "job_id": job["job"]["job_id"], "assessment_evidence_id": assessment_id,
            "job_evidence_id": job["evidence_id"]}) for job in jobs])

    submissions = []
    for calculation in calculations:
        citations = [calculation["calculation_id"]] + calculation["dependency_evidence_ids"]
        submissions.append(("submit_recommendation", {
            "title": calculation["job_title"] + "：可尝试的探索方向",
            "explanation": "根据演示测评和原型规则，可将这个方向列为体验候选；这不能证明实际技能或录用机会。",
            "calculation_id": calculation["calculation_id"], "evidence_ids": list(dict.fromkeys(citations)),
            "limitations": ["团队演示假设，尚未验证职业效度", "未评估真实技能、经历与招聘条件"],
            "next_action": "先完成相关岗位的小任务，再根据体验与成果调整方向。"}))
    return _reply(submissions)
