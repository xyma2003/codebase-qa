# util-linux 三组高危的调用隔离 · 2026-11-20

基线为 `ea3679a` 的成功 CI：Python 3.12.13、Mac 193 / Linux 196 包及 Debian 13 配置保持不变。模型和密钥验收不在本项范围内。

三个公告分别涉及挂载后处理、nsenter 的特权 cgroup 文件描述符、子目录挂载路径解析：

- [CVE-2026-76642](https://security-tracker.debian.org/tracker/CVE-2026-76642)
- [CVE-2026-78408](https://security-tracker.debian.org/tracker/CVE-2026-78408)
- [CVE-2026-78409](https://security-tracker.debian.org/tracker/CVE-2026-78409)

2026-11-20 官方跟踪仍将 Trixie 2.41.5-0+deb13u1 标为 vulnerable/no-dsa。应用不需要挂载或进入其他命名空间。本项不混装 unstable、不强删 essential 包、不自行宣称上游补丁完成。

镜像构建去掉 `/usr` 下全部普通文件的 SUID/SGID 位，将 root 所有的 mount、umount、nsenter、unshare 设为 000。运行时检查这四个工具的权限、所有者、无 Set-ID 文件、UID 10001、有效/许可/边界/环境 capabilities 全空和 NoNewPrivs=1。Linux 烟测实际尝试执行这四个工具，要求 permission denied，并调用 unshare(CLONE_NEWNS)，要求 EPERM。

这属于减少可调用入口，**不是 util-linux 源码修复**。libmount 等库与包记录仍保留，底层扫描命中不会删掉或豁免，发布检查继续阻断。容器逃逸、宿主机内核和特权运维人员绕过部署约束不在已验证范围内。

本机覆盖权限恢复、恶意符号链接和新 Set-ID 文件拒绝；Mac 沙箱自动清除 Set-ID，故该项拒绝逻辑使用明确的 stat 样例，实际镜像文件与系统调用由 Linux CI 验证。首次本机用 chmod 构造 Set-ID 的测试未触发预期，已保存原错误并按实际文件行为修正测试。

本机全量 395 项测试通过，128.196 秒，进程返回 0；摘要见[结构化记录](20261120-util-linux-isolation.json)。实际 Linux 结果在提交后回读，不用本机测试代替容器验收。
