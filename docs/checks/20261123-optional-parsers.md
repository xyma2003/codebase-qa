# 禁用不需要的解析入口

在最近成功配置 `7c4beb9` 的相同 Python 镜像、195 包 Linux 锁和系统安装命令上，仅增加构建后的功能权限限制；上一项 Git 加固已单独提交 `c42042a`。不换发行版、不混装 unstable、不删除包库存、不增加扫描例外。

- [CVE-2025-69720](https://security-tracker.debian.org/tracker/CVE-2025-69720)位于 infocmp 的 analyze_string，Debian trixie 仍标 vulnerable。将项目用不到的 `/usr/bin/infocmp` 设为 000，保留 ncurses 库。
- [CVE-2026-9538](https://security-tracker.debian.org/tracker/CVE-2026-9538)位于 Perl Archive::Tar，trixie 仍标 vulnerable，公告注明等待上游回归整理。将所有实际安装的 Archive/Tar.pm 设为 000，保留 Git 所需的 Perl 与 Error 模块。不是删除整个 Perl，也不手工替换系统模块版本。

构建时发现模块缺失或为符号链接即失败。CI 新增独立运行探针，在 UID 10001、只读、无网络、无 capabilities、NoNewPrivs 的实际候选容器中检查 root 所有、不可读不可执行，实际 infocmp 执行被系统拒绝、Archive::Tar 导入报权限拒绝、Error 模块和 Git 启动仍正常；报告归档为 `optional-parser-probes.json`。原生依赖、TLS/WS、旧 Chroma→SQLite、双用户和恢复检查继续运行。

本机：8 项定向测试通过；全量 **427 项 / 91.350 秒 / 退出 0**；actionlint 和 diff 检查通过。覆盖多个已安装模块副本、权限回退、模块缺失和符号链接。Linux 实际结果在此提交后回读，不能用本机夹具代替容器验证。

此项只禁用两个非必要功能。原文件与包数据库保留，完整系统公告不减少、不忽略；报告明确 `upstream_patched=false` 和 `public_release_allowed=false`。真实模型与 OIDC 请求未执行，原数据未动。
