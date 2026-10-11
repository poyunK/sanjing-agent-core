# Agent 操作指南

## 看板：先登记，再决定何时派单

```powershell
python .agents/skills/local-agent-handoff/scripts/handoff.py add --project . --spec .handoff-local/drafts/task.json
python .agents/skills/local-agent-handoff/scripts/handoff.py revise --project . --task-id 任务ID --spec .handoff-local/drafts/revised.json
python .agents/skills/local-agent-handoff/scripts/handoff.py pack --project . --task-id 任务ID
python .agents/skills/local-agent-handoff/scripts/handoff.py board --project .
```

`add` 不占用执行权，可以在另一任务执行时登记待办。`pack` 才开始串行占用。有前置任务时填 `depends_on`；有明确阻塞条件时把原因写入 context，而不是假装任务已完成。状态通过命令转换，用户不编辑看板文件。

修改 backlog 时保留每版 spec，已派出的任务不可修订。`rework` 留在未完成区域，重新派单需新建任务并写 `parent_receipt`；依赖旧任务的后续事项也要明确改成新的前置任务，不能自动把失败任务算作已验收。

`status --task-id` 只读取指定任务，适合长久使用后的会话恢复。不要将 BOARD.json 中所有历史任务注入上下文。页面是静态 HTML，不启动服务，不包含自动执行的按钮；Agent 执行命令后更新文件，用户刷新即可查看。

## 派单

GUI 标签由实际用户决定，自由命名。不需要另一个 Agent 的会话 ID、API Key 或项目记忆文件。

先检查 Git 状态、当前源码和任务相关文档。项目中的未提交工作属于现有基线：不要擅自 reset、stash、commit 或清理。若用户只说“请给另一个 Agent 做”，在信息充分时直接生成任务；只有关键目标或授权缺失才补问。

脚本路径始终相对 Skill 目录；从其他工作目录调用时传绝对脚本路径。规格和报告可放在项目 `.handoff-local/drafts/`，该目录只用于 Agent 准备文本，不要求用户编辑。

```powershell
python .agents/skills/local-agent-handoff/scripts/handoff.py status --project .
python .agents/skills/local-agent-handoff/scripts/handoff.py pack --project . --spec .handoff-local/drafts/task.json
```

任务把“为什么要做、什么已经确定、改哪里、验收什么”说清楚；引用当前文件路径和真实可复验的检查。不能把前一次验收日期下的结果说成刚刚运行。

任务发出后，本 GUI 不再修改该工作副本；可以进行不写文件的阅读、研究或等待。标准库的真实输出告诉你该交给用户的 TASK.md 路径。任务文本开头也给出了 Skill 路径，陌生 Harness 无需原生技能发现能力。

## 执行

```powershell
python .agents/skills/local-agent-handoff/scripts/handoff.py inspect 接到的TASK.md
python .agents/skills/local-agent-handoff/scripts/handoff.py claim --project . --task 接到的TASK.md --actor deepseek-harness
```

可以读取用户转交的文件副本，但任务 JSON 必须与本地不可变归档一致。仅从截图、摘要或缺了一半的粘贴不能领取。

`stale_snapshot` 表示派单后文件或 HEAD 有变化；不自行刷新快照。把原因回告给用户可转交的阻塞说明，请派单方重新核对。`active_task` 表示已有占用；不能直接删除 ACTIVE.json。`already_claimed` 表示任务已开始，应读取当前状态和实际改动，从那里恢复，不重复执行。

真实执行仍由 Agent 的已有工具完成。脚本仅在 claim 前和 finish 时验证；它不是持续的监控进程。遇到范围外的新需求，用 `partial` 或 `needs_context` 返回，不自改任务规格。网络研究、付费 API、发布等受 `external_actions` 与用户已有授权约束；权限不足不是工具故障。

```powershell
python .agents/skills/local-agent-handoff/scripts/handoff.py finish --project . --task 接到的TASK.md --actor deepseek-harness --report .handoff-local/drafts/result.json
```

如果执行方缺上下文，结果填具体 `context_requests`，不是让用户自己解释源码；派单方读取后准备下一份有补充信息的任务。

越界改动时 finish 会拒绝。在确认改动归属前不要自动覆盖恢复，更不要把别人的改动当成自己的撤销。执行方报告当前差异和冲突，由派单方核对；必要时在用户停止所有写入后协调修复。

## 接收与恢复

```powershell
python .agents/skills/local-agent-handoff/scripts/handoff.py check --project . --message 接到的RESULT.md
```

成功后查看任务验收标准、结果差异和实际当前文件。基线可能包含未提交改动，单纯 `git diff HEAD` 会混入派单前修改；以任务中的 before 哈希和实际 changed_files 为交接边界。必要时用源码、测试输出和 Git diff 辅助解释。

验收时不重复修改接收文件。需要修复，先以 `rework` 关闭这轮并生成新任务，或关闭后在用户授权范围内自己修复；再运行相应检查。

```powershell
python .agents/skills/local-agent-handoff/scripts/handoff.py accept --project . --result 接到的RESULT.md --review .handoff-local/drafts/review.json
```

同一结果再次转回，返回已有回执路径，并说明已经处理。代码提交和 GitHub 上传是单独动作；没有授权时不顺手发布。

新会话或上下文压缩后：

1. `status --project .` 读取 active、任务阶段和已有回执路径。
2. 只读当前 TASK.md / RESULT.md 和任务相关源码；之前的私有记忆不是必需条件。
3. `running` 且自己是执行方：检查已有改动后继续同一任务，不重新 claim；不确定另一 GUI 是否仍在运行时先停止写入。
4. `returned` 且自己是派单方：校验、验收，不再执行一遍任务。

取消任务时必须有真实用户取消指令，执行方已经停止，且派单方核对当前文件。运行 `cancel --project . --task TASK.md --note 取消原因与停止证据` 只记录取消并释放协作占用，不杀进程、不删除改动、不回滚文件。不能把超时或“好像不在运行”当作自动取消依据。

取消尚未派出的待办使用 `cancel --project . --task-id 任务ID --note 用户取消原因`，不会释放其他正在运行任务的占用。

若多文件状态转换被崩溃中断，**先确认相关 GUI / Python 操作已经停止，检查现存档案和实际源码**，再运行：

```powershell
python .agents/skills/local-agent-handoff/scripts/handoff.py recover --project . --task-id 任务ID --note "已核对停止证据及现存归档：具体说明"
```

recover 依据不可变 TASK/RESULT/RECEIPT 修复阶段；没有结果时保留原执行阶段，不重新做任务。它只移除已核对的空操作锁并修复元数据，不杀进程、不回滚源码。存在别的活动任务、越界差异或版本冲突仍拒绝恢复。参数中的停止证据是调用 Agent 的记录，不是脚本能独立证明的进程状态。

## 迁移与成本

复制完整 `local-agent-handoff/` Skill 到另一个项目支持的位置，再在那个项目的指令文件或初始提示中指向它。代码没有 SanJAgent 专用逻辑；`sender` / `receiver` 无厂商枚举限制，非 Git 项目也能用。

同一任务不跨两个工作副本执行；不同 checkout、worktree 或新电脑要重新派单，不能绕过项目路径验证。如果需要远端只拿附件推理，使用原来的 local-web-handoff 或另设计源码传输，不把哈希当原文。

Skill 主入口与当前任务优先加载，细则按角色读取。每轮任务包含必要背景；长期决定应进入项目文档并随代码维护，不把全部历史堆进任务文件。不需要模型 API、MCP 或 GUI 自动化。
