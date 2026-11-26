# Issue tracker

工程工单与规格统一发布到 Multica 的 `memory-agent` 项目，项目 ID 为 `<MULTICA_PROJECT_ID>`。

## 建单与指派

- 在项目看板新建工单，将规格正文或执行要求写入工单描述。通过 Windows 运行时 CLI 建单时，使用 `& "$env:MULTICA_CLI_PATH" issue create --project <MULTICA_PROJECT_ID> --title "<标题>" --description-file "<UTF-8 正文文件>" --status todo`。
- 在工单的负责人字段选择具备相应职责的人或代理。适合由小队承接的工作，指派给「记忆系统小队」。
- 有前置依赖时，在工单正文写入 `Blocked by: MEMO-x`，将 `MEMO-x` 替换为实际工单编号。每项依赖可单独占一行。

## 状态流

待办（`todo`）→ 进行中（`in_progress`）→ 审核中（`in_review`）→ 已完成（`done`）。开始执行时进入进行中，交付待验收时进入审核中，验收后标为已完成。

本项目不需要 `ready-for-agent` 标签或 triage 标签映射。
