# 从你学过的 Agent 循环，读懂这次改动

这篇由主代理撰写。先记住一句话：**模型选择下一步，程序执行并留凭据，建议从已提交账目中发布。** 换成 Ark 后，这个分工没有变。

你不必一次读懂所有模块。先运行 `start.bat` 的选项 1 或 5，看到工具名称、证据 ID 和 HTML 文件，再沿下面的数据路线读代码。

## 一次问题现在经过哪些函数

```text
cli.main：读入问题
  → agent.run_turn：领取会话执行权，检查是否重复请求
  → agent._run_owned_turn：开始你熟悉的工具循环
  → context.build_messages：准备本轮有预算的消息列表
  → model.ask_llm：转换成 Ark Responses 请求并发送
  → tools.execute_tool：先校验、再执行、自动记账
  → messages.append：将对应的工具结果回给模型
  → tools._submit_decision / _submit_recommendation：检查并保存建议
  → agent.render_recommendations：从账目生成正式回复
  → viewer.write_snapshot：保存这一轮的“为什么”页面
```

以前 `run_turn` 直接进入循环。现在它先做一件外围工作：防止同一会话重复执行。原来的循环放进 `_run_owned_turn`，读循环时从这个函数开始就好。

## 为什么不能只替换模型名

你的教材使用 Chat Completions：助手消息中有 `tool_calls`，工具结果是 `role="tool"`。Responses 在网络上使用另一种字典形状。

例如模型提出读取依据：

```python
# Responses 返回的一个 output 项；fc_... 是输出项编号，call_... 才是调用配对编号。
{
    "type": "function_call",
    "id": "fc_example",
    "call_id": "call_example",
    "name": "get_record",
    "arguments": '{"record_type":"evidence","record_id":"ev_example"}'
}
```

`model.parse_responses_response` 把它转成内核已经认识的结构：

```python
{
    "id": "call_example",
    "type": "function",
    "function": {
        "name": "get_record",
        "arguments": '{"record_type":"evidence","record_id":"ev_example"}'
    }
}
```

主循环照旧运行 `execute_tool(ctx, call)`，再把结果加入 `messages`。下一次发送前，`model._responses_input` 把工具结果转换为：

```python
{
    "type": "function_call_output",
    "call_id": "call_example",
    "output": '{"ok":true,...}'
}
```

这样改动集中在模型适配层，你学过的列表和分发字典仍然有效。一次返回多个工具时，每个结果都与自己的 `call_id` 配对；同时有文字和调用，主循环先处理调用。

部分模型还返回私有续接项。本工程只在内存保留必要的加密内容及编号，用于同一轮的协议续接；不保存模型内部思考文字，也不把这部分展示为账本依据。`store:false` 是发送的请求参数，不是对供应商后台保留行为的独立审计证明。

协议依据：[火山 Agent Plan Responses 配置](https://docs.volcengine.com/docs/ark/agent-plan-enterprise-opencode?lang=zh)、[OpenAI 函数调用文档](https://developers.openai.com/api/docs/guides/function-calling)、[无服务端存储的 reasoning 续接](https://developers.openai.com/api/docs/guides/reasoning)。火山具体兼容行为仍以验收记录为准。

## API 无状态，为什么还能记住上一次

API 不替我们的 Python 程序保存业务会话。SQLite 则是我们自己的持久化记忆，两件事并不冲突。

本轮消息列表是临时工作包；数据库是长期档案。`context.build_messages` 读取最多三十条当前条件索引、五个已发布建议 ID，以及最多四十个近期完整回合。旧对话按总长度取，不先把全部历史读入内存。预算不够时整个回合退出工作包，不把半个回合截断后拼进去。

更早的记录没有被删除。新增的 `list_history` 工具只返回本会话的分页编号；模型再用 `get_record` 读取需要的记录。看见编号不代表读过原文，也不会直接得到引用权限。

新条件明确用 `supersedes_evidence_id` 替代旧条件。数据库先排除已经被确认替代的记录，再取近期记录；待确认提取不会自动替代旧自述，更不会修改测评分数。历史建议保留当时的输入。

## 重复请求和多轮对话有什么区别

假设网页按钮卡了一下，浏览器把同一条提交发了两次。没有去重时，后端可能调用两遍模型、保存两套建议。

现在调用方给**一次用户操作**分配一个 `request_id`。同一次操作的重发使用相同 ID；用户重新提问或主动重试使用新 ID。它不是问题文本，也不是模型调用 ID。

下面直接调用工程真实函数，完全离线：

```python
from pathlib import Path
from sanjing.agent import run_turn
from sanjing.offline import ask_offline

db = Path("runtime/learning-requests.sqlite3")
first = run_turn(None, "探索用户研究", db_path=db,
                 client=ask_offline, request_id="button-001")
again = run_turn(first["session_id"], "探索用户研究", db_path=db,
                 client=ask_offline, request_id="button-001")
print(first["turn_id"] == again["turn_id"])  # True：同一份已保存结果
print(again["replayed"])                    # True：没有再请求模型
```

再次传 `None` 会新建会话，所以第二次必须用第一份结果的 `session_id`。缓存返回的 `usage` 属于原来那一轮，统计新费用时不能再累加一次。

`store.begin_request` 用一个很短的 SQLite 事务检查占用并登记本轮。模型等待时不持有数据库事务。同会话正在运行则返回 `SESSION_BUSY`；不同会话可分别处理。请求 ID 对应文字变化则返回 `REQUEST_ID_CONFLICT`。

租约大于整轮期限和一次网络等待期限。过期执行不能继续调用工具；旧轮保留为中断记录。相同请求 ID 不会因为中断自动重试收费服务。超时仍可能产生供应商用量，需要用户主动决定新操作。这与 GUI 之间的 `.handoff-local` 源码交接占用是两套不同的机制。

## 账本能帮助你判断什么

`evidence_quality.quality_flags(data)` 接收一条专业记录的字典，检查可以观察到的字段，再产生提示。它不会用模型给一个“可信度百分比”。

| 可观察情况 | 程序的提示 | 你接下来核查什么 |
|---|---|---|
| 有获取时间，没有资料日期 | 日期未知 | 找原文日期，判断是否适合这次问题 |
| 只有论文摘要或片段 | 未阅读全文 | 阅读方法、样本、结论和限制 |
| 企业记录只有名称或行编号 | 主体待确认 | 核对统一社会信用代码、子公司或集团 |
| 行业/财务记录缺少统计期、单位 | 不宜直接比较 | 核对同一时期和同一口径 |
| 查询没有列出风险 | 覆盖范围未知 | 不能把未查到写成没有风险 |

提示进入 `get_record` 的结果、正式建议的限制和“为什么”页面。旧账目也能在重新渲染时显示这些提示，但原来的 HTML 文件和保存的依据不会被改写。

你仍然需要判断“论文讲的是群体相关性，为什么模型拿它指导这个人的岗位选择”。引用 ID、已读字段和成功执行只能查关联；无法自动证明这种推论成立。演示岗位权重仍属于团队假设。

## Key 和模型容量怎么理解

默认 Ark 请求是 `ark-code-latest`，地址固定为 `https://ark.cn-beijing.volces.com/api/plan/v3/responses`。`key_loader.load_ark_key` 只供客户端使用，读取工程外文件。文件可只有一个 Key，也可采用简单 `api_key: "..."` 标量；不支持任意 YAML 对象、标签或执行。

`SANJING_ARK_KEY_FILE` 配置共用文件路径；`SANJING_DATAPRO_KEY_FILE` 可单独指定专业数据文件。环境变量里保存路径，不复制 Key 正文。切旧 DeepSeek 用 `--provider deepseek` 和 `SANJING_KEY_FILE`。

256000 tokens 上下文和 32000 tokens 输出是声明的模型能力；当前工作包预算为 48000 **字符**，默认请求输出上限 1500 **tokens**。字符不是 token。长窗口也不自动提高事实可靠性；有需要时再调整工作包策略和验收，而不是把所有账目都塞进去。

当前长度检查按内存消息字典保守计数，Responses 续接项可能与公开的工具参数重复计数，因而提前触发长度上限；它不是供应商精确 token 计数。用量中的缓存输入和推理 token 只累计供应商报告的数字，未报告请求单独记录；这些数字不是模型思考内容。

## 四个小练习

先预测，再运行；尽量在独立的 `runtime/learning-...` 数据库练习。

1. 运行上面的重复请求例子，再把 `again` 的问题改为“改成探索产品助理”，保持请求 ID。预测会不会调用模型。
2. 恢复原问题，把请求 ID 改成 `button-002`。预测它是旧结果还是新一轮。
3. 读 `tests/test_evidence_quality.py`，把合成研究记录的 `full_text` 改为空字符串。预测是否出现 `summary_only`。
4. 读 `tests/test_bounded_context.py`，观察 `list_history` 返回的编号。预测它会不会直接进入 `allowed_evidence_ids`。

答案：第一项返回 ID 冲突，没有新调用；第二项是新操作，会创建新轮；第三项再次出现摘要限制；第四项不会授权，必须通过 `get_record` 读取。定向运行：

```powershell
python -m unittest tests.test_session_requests tests.test_bounded_context tests.test_evidence_quality -q
```

准备新增只读能力时，再按 [维护指南](维护指南.md) 增加处理函数、注册声明和对应边界测试。优先能看懂输入输出，再增加更复杂的检索或评分。
