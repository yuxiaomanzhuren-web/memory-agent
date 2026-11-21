# 与本机应用相关的旧件（不属于记忆核）

这里放的是「跑在某台机器上的那套应用」留下的东西，**不是库的一部分**，装 `memory-agent` 不需要它们：

| 目录/文件 | 原来是什么 |
| --- | --- |
| `dsh/` | DSH 前端相关的本地改造与角色包 |
| `life/` | 「生活循环」：让角色自己选书、写感想的那套调度 |
| `console-v2/` | 本机控制台的前端服务 |
| `dsh_slot_shim.py` | 把 DSH 的 node 脚本包成本机 loopback 接口的小壳 |
| `purge_parents_20261102.py` | 一次性维护脚本（已跑完，留作痕迹） |

移动原因：这些东西让仓库根目录看起来像一个工作区，而不是一个可以 `pip install` 的库。
它们依赖本机的路径、服务和账号，别人照着 README 走只会卡住。

如果你想看「这套记忆核在真实场景里怎么被用」，先读 `docs/` 与根目录 README；
想复现本机那套应用，得自己准备对应的服务，这里只提供参考实现。

## `tests-astrbot/`

`test_engine.py` 依赖 AstrBot 插件包（`astrbot_plugin_memory_brain`），属于本机那套应用，不属于记忆核，
所以没跟 `memory-core/tests` 混在一起。跑它需要先装插件。

`verify_memory_three.py` 是早期在本机跑的「记忆三条」验收脚本，需要本机的会话范围配置（`bot_id` 等），
换台机器会直接 `invalid_bot_id`。留在这里当历史，不作为库的测试入口。
