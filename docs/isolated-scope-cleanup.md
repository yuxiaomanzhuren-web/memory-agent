# 隔离测试会话的整区清理

`memory_core.isolated_scope` 在本机登记一个新的专用测试会话，并建立
`industrial_synthetic` 与 `production_snapshot_copy` 两个独立数据区。
两区使用同一完整 scope，各自有存储、索引和进程锁。初始化时生成实例和
会话标识，只接受尚不存在的根目录。已经运行的实例、已有目录及生产
scope 无法登记。请在本机选用仅受管操作账号可写的父目录。

## 启动并写入一个数据区

在已安装此仓库的环境中，分别设置 `MEMORY_ISOLATED_OPERATOR_TOKEN`
和 `MEMORY_AGENT_HTTP_TOKEN` 为随机生成的 32–256 字符可打印 ASCII 密钥，且不含空白。
第一个密钥用于本地受管操作，第二个用于 HTTP 入库和查询。以下为
PowerShell 示例。

```powershell
$isolatedRoot = Join-Path $PWD ('memo33-' + [guid]::NewGuid())
$enrolled = python -m memory_core.isolated_scope --root $isolatedRoot init | ConvertFrom-Json
$destination = [ordered]@{
    instance_id = $enrolled.instance_id
    scope = $enrolled.scope
    area = 'industrial_synthetic'
}
$targetFile = Join-Path $isolatedRoot 'control/target.json'
$destination | ConvertTo-Json -Depth 4 | Set-Content -Encoding ascii $targetFile
Get-Content -Raw $targetFile |
    python -m memory_core.isolated_scope --root $isolatedRoot serve --port 0
```

`serve` 在前台运行，输出本机回环 URL，可用现有 HTTP 适配器执行
`/ingest`、`/recent` 和 `/recall`。请求使用
`Authorization: Bearer <MEMORY_AGENT_HTTP_TOKEN>`。HTTP `conversation`
的 `source_id` 等于 scope 的 `bot_id` 去掉 `agent:`，`owner_id` 等于
`persona`，`conversation_id` 等于 `conv_key` 去掉 `private:`。
处理器写入的原文、SQLite（含索引）和追踪日志均位于该区。
需要核查另一数据区时，用同一个登记结果并改用 `production_snapshot_copy`。

离线导入程序可用 `ManagedInstance(root, operator_token).open_area(target)`
取得同一受管租约。在租约内完成一次导入并关闭所有句柄，可把快照放在该区的
`snapshots/`。同一区正在运行 HTTP 服务时，需先结束服务再开始离线导入。
保存独立索引和日志时使用数据区内的 `indexes/` 和 `logs/`。

## 删除并核对回执

在另一个终端使用同一根目录和操作密钥，指定**一个**数据区、完整 scope、
实例和新生成的 UUIDv4。保留本次请求 JSON，用它重试或查询。

```powershell
$destination = Get-Content -Raw (Join-Path $isolatedRoot 'control/target.json') | ConvertFrom-Json
$request = [ordered]@{
    instance_id = $destination.instance_id
    scope = $destination.scope
    area = $destination.area
    request_id = [guid]::NewGuid().ToString()
}
$body = $request | ConvertTo-Json -Depth 4
$body | python -m memory_core.isolated_scope --root $isolatedRoot delete
$body | python -m memory_core.isolated_scope --root $isolatedRoot status
```

首次受理即封存该区。服务停止接收新请求，等已接收的操作结束并关闭
SQLite 和日志后释放租约。删除最多等待十秒获取租约，可用
`--wait 0` 在已有租约时立即返回当前进度。若返回 `pending` 或 `retryable`，检查
`audit.error` 并用同一请求重试 `delete`。成功时 `status` 和重试
`delete` 均返回原回执。同 ID 换目标，同区换 ID，或在封存后重新启动
该区服务均会被拒绝。

按 `request_id` 配对 `audit` 和 `receipt`，核对 `target`（实例、数据区和
scope 摘要）、`status`、`deleted_files`、`verified_at` 和 `remaining`。
`deleted_files` 统计关闭存储后、删除开始前盘点的文件。关闭 SQLite 时
自行移除的旁文件不计入盘点。两区分别递交删除请求，审计和回执留在根目录的
`control/` 中。完成回执记录应用存储及索引入口的清空核验。

## 合成验收

在仓库根目录用已安装测试依赖的 Python 运行以下命令。可将
`MEMORY_ISOLATED_ACCEPTANCE_EVIDENCE` 指向新建报告文件，保存两个数据区
的实际脱敏审计、回执和生产访问计数。

```powershell
$env:PYTHONPATH = Join-Path $PWD 'memory-core'
python -m pytest -q memory-core/tests/test_isolated_scope.py
```
