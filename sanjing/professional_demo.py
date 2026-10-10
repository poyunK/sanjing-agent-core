"""专业查询的离线接线练习。所有记录都是合成样例，两个函数都不读 Key。"""

import json
from .offline import _reply


def demo_data_client(query):
    """代替网络返回相同形状的字典；用于学习流程，不能据此评价真实企业。"""
    common = {"provider": "合成演示（没有网络请求）", "date_published": "2026-01-01",
              "url": "https://example.org/sanjing-demo", "notice": "合成演示，非真实数据"}
    if "研究与学术" in query or query.startswith("科研学术"):
        category = "academic_search"
        items = [{**common, "title": "合成论文：人格与工作绩效的研究边界",
                  "authors": ["演示作者"], "year": 2026,
                  "snippet": "这是一段合成摘要；不能证明人格测评能决定个人职业。"}]
    elif "行业与宏观" in query or query.startswith("宏观"):
        category = "industry"
        items = [{**common, "title": "合成地区行业背景", "region": "演示地区",
                  "period": "2025", "indicator": "合成指标", "value": 12, "unit": "%"}]
    elif "企业经营与风险" in query or query.startswith("企业风险"):
        category = "company_risk"
        items = [{**common, "name": "合成企业甲", "risk_note": "本次合成样例未列出事件，不代表无风险"}]
    else:
        category = "company_profile"
        items = [{**common, "name": "合成企业甲", "status": "存续（合成）", "founded": "2019-01-01"},
                 {**common, "name": "合成企业乙", "status": "存续（合成）", "founded": "2022-01-01"}]
    return {"code": 0, "msg": "success", "trace_id": "offline-professional-demo",
            "dataset_type": category, "items": items, "demo": True}


def ask_professional_demo(messages, definitions):
    """模拟模型选择查询 → 读取字段 → 提交。所有工具仍由真实入口校验、执行和记账。"""
    user_index = max(i for i, row in enumerate(messages) if row["role"] == "user")
    question = messages[user_index]["content"]
    names, results = {}, []
    for row in messages[user_index + 1:]:
        if row["role"] == "assistant":
            for call in row.get("tool_calls", []):
                names[call["id"]] = call["function"]["name"]
        elif row["role"] == "tool":
            results.append((names.get(row["tool_call_id"]), json.loads(row["content"])))
    if not results:
        if any(word in question for word in ("论文", "学术", "人格", "科学")):
            requests = [("research", "职业人格测评研究的适用边界。")]
        elif "行业" in question:
            requests = [("industry", "演示地区的行业指标，明确统计期和单位。")]
        else:
            requests = [("company_profile", "比较合成企业甲与合成企业乙的登记资料。"),
                        ("company_risk", "合成企业甲的经营风险记录。")]
        return _reply([("query_professional_data", {"category": category, "query": query})
                       for category, query in requests])
    queries = [result for name, result in results
               if name == "query_professional_data" and result.get("ok") and result["data"].get("items")]
    if not queries:
        return _reply([("ask_user", {"fields": ["goal"]})])
    if not any(name == "get_record" for name, result in results):
        return _reply([("get_record", {"record_type": "evidence",
                        "record_id": result["data"]["items"][0]["evidence_id"]}) for result in queries])
    citations = [{"evidence_id": result["data"]["items"][0]["evidence_id"],
                  "field_path": result["data"]["items"][0]["field_paths"][0]} for result in queries]
    return _reply([("submit_decision", {
        "title": "专业依据账本的离线练习", "options": ["先核实要求", "收集更多经历证据"],
        "claims": [{"kind": "fact", "text": "合成资料中的原始字段", "citations": citations},
                   {"kind": "inference", "text": "先核对资料的适用范围，再决定是否用于个人选择。",
                    "citations": citations}],
        "unknowns": ["所有资料为合成样例，不能形成真实职业判断", "真实岗位要求与用户经历尚未核实"],
        "next_actions": ["打开为什么页面，逐条核对字段、来源和限制"]})])
