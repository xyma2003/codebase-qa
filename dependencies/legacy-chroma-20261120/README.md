# 旧 Chroma 环境锁

逐字节保留 `b2d98ab` 成功 CI 所用 Mac 193 包和 Linux 196 包锁，摘要见 manifest.json。仅用于离线导出、临时兼容测试和完整数据回滚，不是当前部署输入。原始阶段零 194 包锁另在 docs/baseline/runs/20261110-stage0/，没有覆盖。

此环境含 Chroma 的 4 组未修复公告，不能用于公开服务。当前运行锁只移除 chromadb==1.5.9，其他版本与哈希不变；Mac 为 192 包、Linux 为 195 包。应用已改用 SQLite + NumPy，迁移见 docs/VECTOR_MIGRATION.md。
