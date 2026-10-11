---
name: local-agent-handoff
description: 与 Codex、DeepSeek Harness 或任意可读取同一本地项目的 Agent 双向交接 TASK.md 和 RESULT.md，用户只转交文件。用于外包、接手、回执、验收与恢复。
---

本目录是 Claude Code 的发现入口，不维护第二份协议。

先定位项目根，再读取该根目录下 `.agents/skills/local-agent-handoff/SKILL.md`，按那份 LAH/1.0 协议执行。所有脚本、参考文件和模板都在那个规范目录；不要相对本包装目录查找它们。

派单方和执行方可以互换，协议不限定厂商。读取失败时报告缺少协议；不要猜测契约。任务附带的 Skill 绝对路径可用于定位。普通项目工作先检查 `.handoff-local/ACTIVE.json`，避免在其他 Agent 的交接任务中并行写同一工作副本。
