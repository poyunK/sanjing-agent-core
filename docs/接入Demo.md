# 把内核接入聊天 Demo

本工程提供同步函数 `run_turn()`，入口在 `sanjing/agent.py`。它负责本轮工具循环、权限检查、执行事件、证据与建议账本；界面负责接收用户输入、展示状态和回复。

## 先用离线模式验证接线

把下面代码放在能导入 `sanjing` 的 Python 工程中运行。可以先在本仓库根目录测试；离线模式不会读取 Key 或调用网络，但会保存本地演示记录。

```python
from pathlib import Path
from sanjing.agent import run_turn
from sanjing.offline import ask_offline

def show_progress(event):
    print(event["stage"], event["elapsed_seconds"])

first = run_turn(
    None,
    "根据演示测评比较用户研究和产品经理助理，并显示依据。",
    db_path=Path("runtime/demo.sqlite3"),
    client=ask_offline,
    on_progress=show_progress,
)
print(first["status"], first["answer"])
print(first["ledger_path"])

second = run_turn(
    first["session_id"],
    "继续讨论这两个方向。",
    db_path=Path("runtime/demo.sqlite3"),
    client=ask_offline,
)
```

传入 `None` 会创建一个新会话；下一次用返回的 `session_id` 继续。`client=ask_offline` 是固定的演示脚本，不是能理解所有问题的离线大模型。

调用真实 DeepSeek 时，先配置 `SANJING_KEY_FILE`，再省略 `client` 参数。模型和输出限制见 `config.py`，密钥读取只发生在 `model.py`。

## 界面处理三个结果

| 返回状态 | 界面行为 |
|---|---|
| `completed` | 显示已提交的建议和“为什么”入口 |
| `needs_input` | 显示追问，等待用户补充 |
| `incomplete` | 显示失败或未完成原因，让用户主动重试 |

返回字典还包含 `turn_id`、`error_code`、`usage` 和 `ledger_path`。输出分数来自程序计算，解释文字来自经过结构与引用检查的建议记录；不要另外请求模型重写一份报告，从而产生另一套依据。

`on_progress` 接收脱敏后的阶段名称与累计耗时。Web 界面可把它转成进度提示；回调应快速返回，不要在回调中执行长任务。同步函数会等待模型，接入 Web 框架时按框架方式放到后台任务或工作线程，同一会话按顺序处理。

## 会话和账本的访问边界

由服务器把登录用户映射到自己的 `session_id`。不要直接相信客户端传来的会话编号，也不要因为知道一个编号就允许查看别人的账本。本工程的本地会话查询不代替 Web 登录鉴权。

“为什么”入口应按用户身份检查归属后，再展示对应账目。`ledger_path` 是服务端本地文件位置，不能直接当公网下载地址；不要把整个 `runtime` 目录设成公开静态目录。

当前阶段建议先跑通一名测试用户、一个会话、合成资料。确认多轮聊天和依据展示一致，再接真实的测评数据、岗位资料与产品规则。

## 当前能力边界

- 只有本地演示测评、关键词岗位检索与团队原型匹配规则；尚未连接实时招聘或向量库。
- 工具能力通过注册表、参数和会话检查控制；没有任意命令、代码、SQL或文件访问工具。这不是操作系统沙箱。
- 自由模型文字不会直接作为正式建议发布，追问使用固定字段的 `ask_user` 工具。
- 账本核对引用关系和计算记录，不能自动证明岗位匹配规则具有科学效度。
- 没有附带登录页面、完整聊天 Web UI 或 PDF 导出功能。

新增能力先走 [维护指南](维护指南.md) 中的注册与测试流程，不要绕过 `execute_tool()` 从模型响应直接运行代码。
