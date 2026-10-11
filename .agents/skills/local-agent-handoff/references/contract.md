# LAH/1.0 文件契约

适用前提：两边可以读取同一个项目目录并运行本地 Python。没有该能力时用 `needs_context` 报告，不能把本协议误当作云端源码同步。LAH/1.0 借鉴 LAWH/1.0 的快照和父消息关联，但不是其兼容实现。

每个文件由简短中文说明和**唯一一个 `lah-json` 代码块**组成。机器字段以 JSON 为准，正文帮助用户辨认任务。禁止重复 JSON 键、多个契约块和未知协议版本。不要把一个文件的正文附加为另一任务的授权。

## 任务规格：派单方填写，脚本生成其余字段

`pack` 输入 JSON 对象：

| 字段 | 含义 |
|---|---|
| `sender`, `receiver` | 自由命名的 Agent 标签，例如 `codex`、`claude-code`、`deepseek-harness`。不绑定模型、厂商或登录账号 |
| `mode` | `edit`、`review`、`plan`。后两者仅允许生成交接报告，不修改项目文件 |
| `goal` | 单一、可验收的工作目标 |
| `user_authorization` | 本项工作的真实用户指令摘要。不是执行方可以自授的权限 |
| `context` | 接手此任务所需背景，不粘贴整个聊天历史 |
| `confirmed_decisions` | 已确认决定的字符串列表；猜测不能列入 |
| `read_first` | 必需源码/文档的项目相对路径列表。不要列入 Key 或数据库 |
| `write_scope` | 允许改动的项目相对路径列表；目录以 `/` 结束。没有通配符。不含交接自身状态目录 |
| `acceptance` | 可观察的验收标准列表，不能仅写“效果良好” |
| `out_of_scope` | 明确排除的功能或活动列表 |
| `external_actions` | 已由用户明确授权的外部动作列表；默认 `[]`，自然语言要写清动作及必要的额度上限。此字段不自动执行任何动作 |
| `allow_delete` | 可选布尔值，默认 `false`；允许删除时仍仅限 `write_scope` |
| `allow_commit` | 可选布尔值，默认 `false`；不意味着允许 push/reset/改写历史 |
| `parent_receipt` | 可选，返工或继续任务时填写上一回执 ID |
| `title`、`priority` | 可选看板标题和 `high` / `normal` / `low` 优先级 |
| `depends_on` | 可选前置任务 ID 列表；只有这些任务已 `accepted` 才能派单 |

`review`、`plan` 的 `write_scope` 必须为空。`edit` 必须指定具体范围；不要为了省事填整个项目。允许范围覆盖新文件，也覆盖源文件的修改；删除还要 `allow_delete=true`。

生成的 TASK.md 另含：`protocol`、`kind=task`、`task_id`、`message_id`、`created_at`、`skill_path`、`project`、`snapshot`。`project` 包括本地项目 UUID、真实根路径、Git HEAD（非 Git 项目为 null）。`snapshot.files` 是当前非忽略、非敏感源文件的真实 SHA-256；未提交文件也有哈希。`snapshot.id` 对项目 ID、HEAD 和文件映射计算摘要。脚本不自动 commit、stash 或清理改动。

## 执行结果规格

`finish --report` 输入：

| 字段 | 含义 |
|---|---|
| `status` | `completed`、`partial`、`blocked`、`needs_context`、`proposed` |
| `summary` | 实际交付的结果；解释未完成部分 |
| `decisions` | 有限的决定与简短依据列表；不要求或保存内部思维链 |
| `checks` | 检查记录数组，见下方 |
| `remaining` | 未解决问题、未验证范围、风险列表 |
| `context_requests` | 需要派单方补充的具体信息列表；`needs_context` 时不能为空 |

`plan` 使用 `proposed` / `blocked` / `needs_context`；`review` 可以 `completed`，但不把建议说成已改代码。一次测试失败不一定意味着任务失败，例如复现缺陷就是验收目标；报告必须给出真实结果。

每个 `checks` 对象：`command`（实际运行的命令或人工检查名称）、`outcome`（`passed` / `failed` / `not_run`）、`evidence`（短输出或证据位置）、`exit_code`（可选数字或 null）。`not_run` 要说明为什么没运行。脚本不会执行这些命令，也不能证明写入的检查记录真实；派单方按需复验。

RESULT.md 的关联字段和差异由脚本填写：`in_reply_to` 对应任务 `message_id`，`based_on_snapshot` 对应原快照，`changed_files` 记录真实 before/after 哈希，`snapshot` 为完成后的全体源文件快照。禁止手编“测试通过”的输出、文件哈希或 Git SHA。

## 验收规格

`accept --review` 输入：`decision`（`accepted` / `rework` / `rejected`）、`summary`、`checks`。生成 `kind=receipt` 的 RECEIPT.md，关联结果与任务，记录派单方实际验收。`accepted` 不是安全认证，`rejected` 也不会撤销已经存在的修改。

## 状态与冲突

```text
backlog → offered → running → returned → accepted / rework / rejected
    └────────── 用户取消并确认执行已停止 ──────────→ cancelled
```

占用从派单开始，直到接收结果并记录验收。状态为 `running` 时换会话，从当前文件恢复，不重新领取。返工用新任务、新快照和 `parent_receipt`，不能改旧任务悄悄扩大范围。

脚本拒绝任务内容与本地归档不一致、快照变化、项目路径不符、越界改动、重复领取和伪造关联。重复接收已验收的相同结果会返回已有回执，不重新应用。`.handoff-local/` 需要被 Git 忽略；不因“本地”就可以放秘密。

源码验证只覆盖脚本列入快照的文件：Git 项目使用 tracked + nonignored untracked，非 Git 项目按排除表遍历。运行目录、缓存、密钥等不进入快照；不能用它证明这些区域没有被改动。SHA-256 证明字节一致，不证明资料可信、任务授权真实或自然语言结论正确。

## 长期看板与权威记录

每项任务都有 `state.json` 和按编号保存的原规格。`add` 登记 backlog；`revise` 仅能修改 backlog，派单时才获取真实代码快照。依赖关系不允许环路，尚未验收的前置任务会阻止派单。

BOARD.html / BOARD.md / BOARD.json 是这些记录的派生视图，不是需要人手维护的第二套记忆。历史 TASK、RESULT、RECEIPT 不改写。源码和项目长期决定仍放源码/项目文档，看板保存任务进展和交接索引；不要把全部对话或个人数据库放进看板。

时间存储为 UTC ISO 8601；需要向用户口头报时间时转换为用户所在时区。这里不是联网协作平台，没有自动消息、通知或云同步。
