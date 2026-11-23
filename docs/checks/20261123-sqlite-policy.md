# SQLite 连接限制

配置沿用本轮真实成功基线：Python 3.12.13、既有 Mac/Linux 哈希锁、既有 Python 镜像和系统包安装命令，不引入新依赖。前两项 `c42042a` 与 `28ac3e8` 的 Mac/Linux CI 均已成功，最近为 [45154476997](https://github.com/xyma2003/codebase-qa/actions/runs/45154476997)，Linux 产物 SHA256 `406f10d897f5b355d640f1abe52099b8af236873332e22c1e59193e91d783c05` 已核对。

6 处业务与运维连接统一到 `security/sqlite.py`。业务库启用 SQLite 原生 DEFENSIVE，关闭 trusted_schema、扩展加载和 FTS3 tokenizer；配置必须能回读确认。authorizer 拒绝虚表、ATTACH/DETACH、MATCH、扩展函数和危险 PRAGMA；已有虚表或 shadow 表的文件也不能作为业务库打开。会话、任务、版本和向量原 SQL、超时、事务和结构不变。

旧 Chroma 数据有 FTS5，不能删掉或跳过完整性检查。因此备份使用单独封装：`mode=ro`，只执行固定的 `PRAGMA integrity_check`，禁止写入、MATCH、附加库和扩展；不向调用方返回连接。已有损坏库仍拒绝备份，不把异常当作成功。

本机完成 **70 项定向测试**、真实 SQLite 3.53.2 探针与 **435 项全量 / 91.400 秒 / 退出 0**，actionlint 和 diff 检查通过。当前原数据 4 个集合/1256 向量、2 会话/3 轮可读，会话内容 SHA256 仍为 `9fd14008178680c699d441cd6e83dd0056b35b1f4c0faf206df92afec7de647f`；保留的原 Chroma 库通过只读完整性检查。CI 在实际 Linux 引擎上另跑 `ops.sqlite_security_check`，归档 `sqlite-security-probes.json`，继续执行旧底座迁移和恢复。

119373/119374 涉及 FTS5 查询或删除；这次限制业务库使用该功能，旧库备份仅只读完整性检查。119379 涉及 native session changeset；现有 Python 3.12 入口没有 Session API，也没有添加 changeset 接收工具。**不声称修复 SQLite C 代码，不是对任意恶意数据库的安全承诺**。系统公告与完整扫描保留，公开发布继续阻断，真实模型/登录后置。
