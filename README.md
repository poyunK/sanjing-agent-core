# 三镜 Agent 内核原型

这是一个适合边运行边读的 Python 教学原型。它把一次 Agent 任务拆成几件普通事情：准备消息列表、请求模型、按白名单分发工具、把工具结果接回消息列表，并把使用过的依据保存到账本。

## 启动

需要 Python 3.10 或更新版本。本工程只使用 Python 标准库，不需要安装第三方包。Windows 用户可以运行 `start.bat`：选项 1 为原版岗位演示，选项 5 为专业查询离线练习，选项 2 为真实 DeepSeek＋DataPro 对话。主循环继续使用 DeepSeek；火山 Key 只用于专业数据 MCP。

### 从 GitHub 下载后的第一次运行

```powershell
git clone https://github.com/poyunK/sanjing-agent-core.git
Set-Location sanjing-agent-core
python -m sanjing.cli --offline --trace
python -m sanjing.cli --professional-demo --trace --once "比较合成企业资料"
```

这里需要本机安装 Python 3.10 或更高版本；仓库没有打包 Python 和模型。离线模式不需要 API Key，使用手写的模拟模型观察工具与账本流程。

要调用真实 DeepSeek，把自己的 Key 保存到仓库之外的 UTF-8 文本文件，再在同一个 PowerShell 中设置文件位置，例如：

```powershell
$env:SANJING_KEY_FILE = 'C:\local-secrets\deepseek.txt'
python -m sanjing.cli
```

路径是示例，替换为自己的文件。不要把 Key 文件放到仓库或提交。调用真实模型会产生服务商用量。

准备接入聊天界面时，阅读 [接入 Demo](docs/接入Demo.md)。当前内核是同步的 Python 函数接口，没有附带完整 Web 服务。

也可以在本目录打开终端手动运行：

```powershell
python -m sanjing.cli --offline
python -m sanjing.cli --offline --trace
python -m sanjing.cli
python -m sanjing.cli --session 这里填写会话ID
python -m sanjing.cli --resume-prompt
python -m sanjing.cli --once "请按现有依据说明一个可探索方向"
python -m sanjing.cli --show-latest
```

离线模式使用固定的演示数据和模拟模型，不调用网络，也不需要 Key。真实模式会发送模型请求；每轮有请求次数与超时上限。交互中可用 `/why` 查看本轮账本路径、`/session` 查看会话 ID、`/exit` 退出。

## 先看哪里

**第一次阅读，请从 [先读这篇：从教材到工程](docs/先读这篇_从教材到工程.md) 开始。** 它从你学过的第 9 章接过来，逐层展开消息、工具参数和账目；第一次不需要同时理解所有模块。

之后用 [代码阅读路线](docs/代码阅读路线.md) 作为工程索引。最常用的程序入口是 `sanjing.agent.run_turn(session_id, user_text, ...)`；页面入口是 `sanjing.viewer.write_snapshot(db_path, session_id, turn_id, output_dir)`。

- [数据旅行](docs/数据旅行.md)：跟踪一条消息、一次工具调用和一条账目。
- [维护指南](docs/维护指南.md)：增加只读工具、资料与规则，了解如何运行针对性检查。
- [练习与答案](docs/练习与答案.md)：用小改动观察消息、账本和安全校验。

## 账本页面

每轮会保存独立的静态 HTML 页面，默认放在 `runtime/ledger/`。页面展示结构化建议、输入、规则版本、贡献、证据片段和限制；工具执行记录收在可展开区域。页面不运行脚本、不启动网络服务，也不展示模型原始思维内容。`start.bat` 的“查看最近账本”是用户主动选择的动作，会让本地浏览器打开最近一页。

账本中的关联检查只能说明系统引用了某条记录，不能单独证明引用材料支持结论。岗位映射和权重在演示阶段属于团队原型假设，不代表经过科学验证的职业适配结论。

## 安全边界

模型只能请求程序显式注册的业务工具。它没有任意 Shell、Python、SQL、文件路径或网络访问工具。参数先经过结构校验和权限检查，再调用对应函数。这个白名单是应用层限制，不是操作系统沙箱；以后如果增加命令执行能力，必须另行设计隔离。

`--trace` 用于教学观察。它展示消息角色、调用 ID、工具名和证据 ID等脱敏事件，不应打印 Key 或完整个人资料。开发测试请使用合成数据。

## 配置、状态与后续接网页

模型默认 `deepseek-flash`、非思考模式，单轮最多八次模型请求，每次最多输出 1500 tokens、最多等待六十秒；专业数据最多十六次查询，每次最多六十秒，全轮最多三百秒，不自动重试。可通过环境变量 `SANJING_MODEL` 调整模型名、`SANJING_KEY_FILE` 调整 DeepSeek Key 文件位置、`SANJING_DATAPRO_KEY_FILE` 调整 DataPro Key 文件位置；这些变量只需保存文件路径。

`run_turn` 返回 `completed`（结构和引用校验通过）、`needs_input`（等待补充条件）或 `incomplete`（未完成）。只有成功调用 `submit_recommendation` 或 `submit_decision` 才发布建议；单独输出一段模型文字会被拦截。外部事实与用户自述由程序从引用字段填入，模型的推论明确标为生成分析。

本地 SQLite 保存用户原话、账目和回复。回合内的完整工具协议仅在内存中继续传回 API，历史回合只读取已发布回复和独立记录。未来接网页时，需要服务端登录与会话映射，并串行处理同一会话；本地会话 ID 不是网页登录凭证。

查看本次交付的 [验证记录](验证记录.md)。运行工程无需 Node、Playwright 或第三方 Python 包；`verification/render_check.cjs` 只是开发阶段可选的浏览器验收脚本。

## 专业数据的第一次运行

先运行 `start.bat` 选项 5，打开终端返回的 HTML 路径，核对一条字段引用。所有离线资料均为合成样例。再配置工程外密钥文件，选择选项 2：

```powershell
$env:SANJING_KEY_FILE = 'C:\local-secrets\deepseek.txt'
$env:SANJING_DATAPRO_KEY_FILE = 'C:\local-secrets\datapro.txt'
python -m sanjing.cli
```

本机已有的默认位置分别是学习工作区的 DeepSeek 文件与用户 Documents 中的火山文件；从 GitHub 下载后请使用自己的文件路径。不要把文件或密钥内容放入仓库。

输入示例：`请查询目标公司的登记主体与企业风险，并列出求职前需要继续核实的事项。` 专业数据不是实时招聘库；请自行提供具体公司名、城市和 JD。需要比较行业时写明地区、指标与时间范围。

查询结果是候选资料。正式回复逐条展示采用字段、用户原话、模型分析、缺失项与下一步动作；历史快照保留原查询时间。真实世界的资料适用性仍需核查。

阅读 [专业数据接入与教学](docs/专业数据接入与教学.md) 学习实际数据流；阅读 [专业数据验收](专业数据验收.md) 了解本次真实验证边界。`python -m verification.factual_check` 会离线回放三十个固定案例，并将结构检查与人工内容核查分别记录。
