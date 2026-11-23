# APT 系统保护拒绝与修正 · 2026-11-23

首次精简提交 `d010c7c` 的 Linux [CI](https://github.com/xyma2003/codebase-qa/actions/runs/45146594565) 在构建阶段原样失败，作业 137597290662：

```text
WARNING: The following essential packages will be removed.
apt libapt-pkg7.0 (due to apt)
E: Essential packages were removed and -y was used without --allow-remove-essential.
```

原 9 包最低版本检查已经通过；删除步骤尚未执行成功。这个基础镜像保护 APT，不能按普通无用工具处理。保留原错误和失败产物 14803458940，SHA256 `dfc078b5dab7e76aab8318bd288c05001cd402a0de4a84e222d907e5d72274eb`。

本次不强制删除、不改系统保护标记、不用 autoremove。APT/libapt 及原 9 包版本检查保留，撤销未采用的“运行期删除包”分支；uv 留在构建阶段，全局 pip 移除。运行期将 apt、apt-get、apt-cache、apt-cdrom、apt-config、apt-mark 与原 4 个特权工具一起设为 root 所有、权限 000。扫描仍报告 APT 源码公告，不能记作组件已移除。

Linux 实测要求这 10 个工具直接执行均被拒绝、权限/所有者/非 root/空 capabilities/NoNewPrivs 一致；虚拟环境中依赖只读、安装器不可导入，CPU 原生库与 Git HTTPS 链接完整。受控旧镜像对照改为要求 **所有系统包及版本完全一致**，同一漏洞数据库前后摘要相等，避免借机删包或隐藏公告。

调整基于上述原始错误，单独提交修正后验证，不直接重跑失败作业。最新本机与实际 CI 结果以最终回读为准。前一份 420 项测试记录属于未通过 Linux 构建的候选，不能作为最终镜像验收。

修正本机全量 418 项通过，117.498 秒、退出 0；原 420 项中的两个“运行期删除包”分支测试随未采用分支一起撤销，既有 415 项与 3 项镜像对照回归均保留。actionlint、权限与包检查定向用例通过，Linux 实际结果待修正提交 CI。
