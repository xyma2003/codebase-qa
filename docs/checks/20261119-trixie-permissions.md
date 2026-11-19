# 临时存储卷初始化修复 · 2026-11-19

首次 Debian 13 提交 `1a21695` 的 [CI](https://github.com/xyma2003/codebase-qa/actions/runs/44489442572) 中，Mac 392 项测试通过，Linux 的实际镜像构建、最低版本、196 包安装清单、Python 公告收集及 TLS/WebSocket/限流检查通过。

跨底座检查在创建临时数据目录时失败，原始错误为 `PermissionError: [Errno 13] Permission denied: '/fixture/runtime'`。初始化进程只有 CHOWN capability；先将父目录交给 UID 10001 后，root 进程没有绕过目录写权限的 capability，无法再创建子目录。原失败日志和 CI 产物保留，没有重跑原配置或修改正式服务权限。

修复只调整操作顺序：父目录仍归初始化进程时先创建子目录，再将子目录和父目录交给 UID 10001。只读根、禁网、最小 capability 和实际存储检查用户保持原设置。此变更单独提交；本机 4 项存储平台契约检查、Python 编译及 actionlint 通过，实际跨底座回读和系统复扫由此提交 CI 继续验证。
