# Git 传输入口强制限制

基线：最近成功的 `7c4beb9fca99bf2f4161c78f17526927fa3bae42`，CI [45149919356](https://github.com/xyma2003/codebase-qa/actions/runs/45149919356)。Linux 产物 SHA256 `738148bd6b5341cfba52f0df30cea2f531025e64ab1e1f4b07dc7a2a8907b20b` 已下载核对；镜像 `sha256:b6669ea30ff5b8d8b4fcadb37525e260ac614b255de535af3328711e95a31e7a`，完整扫描 336 条/177 编号，含 21 个不同 High。

Git 官方更新的 [CVE-2026-111760](https://github.com/git/git/security/advisories/GHSA-vp2w-7xxj-jvxr)说明非递归检出也可能受影响。不能把“不加 --recurse-submodules”当成充分防护，因此固定现有 `--no-checkout` 浅克隆，传输函数只接受现有两种精确调用形式。拒绝合并、变基、cherry-pick、merge-tree、checkout/status、fast-import/export、附加参数、非规范化 GitHub HTTPS 地址和越界克隆目录。同时关闭 fsmonitor、hooks、递归子模块、代理和空认证。

本机：33 项 Git/版本定向测试通过；全量 **422 项 / 91.778 秒 / 退出 0**。新增反向测试确认危险参数在创建子进程前被拒绝，正常两种调用继续执行，原大小和超时限制继续生效。锁、底座、模型配置、原数据和发布门槛未变；真实模型请求 0。`c42042a` 的 [CI 45153500387](https://github.com/xyma2003/codebase-qa/actions/runs/45153500387) 已回读，Mac 422 项/157.370 秒、Linux 构建和恢复全通过。

这是应用调用限制，不是 Git 源码补丁。105437、111760、114466 等底层公告保留，未完成真实漏洞载荷回放，不生成 VEX 或发布豁免。
