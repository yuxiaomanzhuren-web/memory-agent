# 受管只读快照导出

MEMO-32 提供本机 CLI，用完整来源 scope 和预批原文 ID 从已有归档索引取样。操作者从标准输入交付一份 JSON 请求。CLI 在登记的隔离区提交唯一文件 `production_snapshot_copy.jsonl`，标准输出只返回脱敏回执。

## 运行条件

- 由本机受管服务账户运行。来源索引、归档及其父目录对该账户开放读取权限，隔离区开放写入权限。登记文件、现有 scope 凭据文件、隔离根及父目录由管理员控制。调用者只提供请求，环境和登记由管理端设置。
- 使用本机专用且未加入同步服务的测试数据区。Windows 目标需位于支持硬链接及关闭时删除的本机固定卷，例如 NTFS。Linux 目标需支持 `O_TMPFILE` 和通过 `/proc/self/fd` 发布硬链接。目录重解析点、符号链接和跨区路径会被拒绝。
- 使用已有 `memory_pipeline_raw` 索引及冲突表。WAL 索引须已有可读的 `-wal` 和 `-shm`，并用来源只读权限运行。缺项请由索引管理员核对归档状态。SQLite 只读打开与 WAL 旁文件的关系见 [SQLite 官方文档](https://sqlite.org/wal.html#read_only_databases)。
- MEMO-28 真实抽查须在完整 scope 删除、审计和回执通过独立验证，并获得切换确认阶段的单独批准后，再登记相应生产来源和预批清单。

## 登记和调用

`MEMORY_SNAPSHOT_REGISTRY_FILE` 指向受管 JSON，`MEMORY_SCOPE_CREDENTIALS_FILE` 指向现有 scope 鉴权登记，`MEMORY_SNAPSHOT_SCOPE_CREDENTIAL` 由管理端注入。以下登记示意使用 Linux 路径和合成 scope。各目录须预先存在，Windows 登记应使用本机绝对路径。

```json
{
  "version": 1,
  "source_index": "/srv/synthetic-source/brain.db",
  "archive_root": "/srv/synthetic-source/raw",
  "isolation_root": "/srv/memory-isolated",
  "areas": {
    "production_snapshot_copy": {
      "test_scope": {"bot_id": "333", "persona": "synthetic-target", "conv_key": "private:444"},
      "directory": "case/snapshot"
    }
  },
  "approval": {
    "source_scope": {"bot_id": "111", "persona": "synthetic-source", "conv_key": "private:222"},
    "test_scope": {"bot_id": "333", "persona": "synthetic-target", "conv_key": "private:444"},
    "area": "production_snapshot_copy",
    "raw_ids": ["synthetic-id-1", "synthetic-id-2"]
  }
}
```

标准输入的 JSON 对象须与 `approval` 相符，含 1 到 1000 个不重复的 ID，依预批顺序排列。PowerShell 可在已安装项目的解释器中运行以下命令。

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Get-Content -Raw -Encoding UTF8 ./request.json | python -X utf8 -m memory_core.managed_snapshot
```

退出码 0 的回执含 `status`、`record_count`、`byte_count` 和整文件 `sha256`。退出码 1 的回执含 `status`、`reason` 和零条已提交记录。可将脱敏回执就地登记为验收证据。隔离区文件的前 N 行保留原始字节，包括时间和来源字段，最后一行登记完整来源及测试 scope、数据区、原文区段 SHA-256 和按行排列的索引来源标签。隔离回放应从同一文件读取这 N 行并检查尾部绑定与哈希。

所有原文及绑定信息随副本留存于已登记数据区。对同一清单重试时，现有文件经整文件哈希核对后一致即返回原回执。任何不同内容都会返回冲突并保留现有文件。发布前的进程退出由操作系统清理临时写入。

## 合成验收

使用项目测试依赖，在仓库根目录运行。测试自行创建来源索引、归档、凭据和登记，并使用独立临时区。

```powershell
$env:PYTHONPATH = (Resolve-Path memory-core).Path
python -m pytest -q memory-core/tests/test_managed_snapshot.py
```
