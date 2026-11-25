# 系统公告的实际入口核对

日期：2026-11-23。依据 `aad406e` 的真实 Linux 产物：336 条、177 个编号、30 组来源组件，0 Critical、65 条 High（21 个不同 High 编号）。每组原始编号、二进制包与版本保存在 CI `risk-review/review.json`，来源是包库存的 SrcName，不靠包名猜测来源。本页是首轮程序入口核对，不是完整利用测试、VEX、豁免名单或发布授权。

## 重点入口

| 来源与公告 | 必需触发条件 | 当前代码与运行配置 | 处理状态 |
| --- | --- | --- | --- |
| util-linux 的 4 个 High：76642、78408、78409、78410 | 特权 mount、nsenter 或授权挂载流程 | root 所有的 mount/umount/nsenter/unshare 权限 000，UID 10001、无 capabilities、NoNewPrivs；实际直接调用与 unshare 已受 CI 检查 | 调用隔离；源码仍未修复。78410 同样跟踪，不漏掉此前三项之外的公告 |
| Git：105437、111760、114466 | merge 尺寸溢出、恶意树检出（也影响普通非递归克隆）、fast-import/export 文件访问 | `security/git_transport.py` 在启动进程前只接受现有两种精确参数：不检出代码的浅克隆、默认 HEAD 浅更新；仅规范化公开 GitHub HTTPS。拒绝 merge/rebase/cherry-pick/merge-tree、checkout/status、fast-import/export、递归子模块和附加参数；关闭 fsmonitor、hooks、子模块递归、代理和环境凭据。克隆目录只能为受资源监控临时目录下的新目录 | 增加代码强制限制与反向测试；底层 Git 未升级，不能称为源码修复或关闭公告 |
| curl：12064、8458 | 扫描说明分别涉及无 scheme 的 SFTP/SCP CLI 和连接认证复用 | 仓库 URL 归一为公开 GitHub HTTPS；Git 协议仅允许 HTTPS，禁用环境代理、重定向及凭据。每个 Git 调用为独立子进程，环境不带模型密钥 | 当前路径受约束；继续保留底层库公告，不把所有 curl 代码都当作已证明不可达 |
| SQLite：119373、119374、119379 | FTS5 索引或 session changeset 路径 | 6 处连接统一启用 DEFENSIVE、关闭 trusted_schema/扩展/FTS3 tokenizer；业务库拒绝已有或新建虚表、ATTACH/DETACH、MATCH 和危险 PRAGMA。保留的旧 Chroma FTS 库只在固定只读完整性检查中打开，不向业务返回连接。Python 3.12 没有 Session API，也没有 changeset 接收工具 | 新增业务入口强制限制与 Linux 引擎探针；native SQLite 仍未打补丁，不关闭公告 |
| OpenSSL：124709 | OpenSSL QUIC 服务端持续接收并重置流 | 代理配置仅 TCP TLS，Compose 没有公开 UDP，Streamlit 上游使用 HTTP/1.1 WebSocket；当前没有 QUIC 服务配置。客户端模型 HTTP 请求不能作为 QUIC 服务端入口 | 配置条件未出现；保持公告。扫描 High 与厂商描述的 Low 应分开记录，不改扫描原级别 |
| systemd：16742 | systemd-homed 的特权家目录记录处理 | 扫描的二进制包是 libsystemd0/libudev1；容器进程为应用及 init，不是 systemd-homed 服务 | 组件库与服务入口需要区分；本结论不覆盖同源其他公告，无自动豁免 |
| Perl Archive-Tar：9538 | 解析恶意 tar 头中的巨大 entry size | Perl 为 Git 的依赖，不删除整个 Perl；构建时将所有 `/usr` 下 Archive/Tar.pm 的权限设为 000，保留包库存；CI 使用 UID 10001 实测导入拒绝，并确认 Git 所需 Error.pm 可用 | 新增模块入口隔离，需以对应 Linux 产物验证；源码公告保留 |
| ncurses：69720 | infocmp 的 analyze_string 解析恶意终端数据 | 仅将用不到的 infocmp 设为 root 所有、权限 000，不删 Python/Git 依赖的 ncurses 库；CI 实际执行要求 PermissionError | 新增命令入口隔离，需以对应 Linux 产物验证；源码公告保留 |
| Expat：113351、115506、117610、118370 | 解析器重置内存计数、实体放大、空外部实体与超大属性计数 | 11 月 25 日增加仅 libexpat1 的官方 2.11 包更新；固定大小/哈希/版本/架构，原 libc6 满足 >=2.38，不启用 sid 源。另查新公告 124317、128241、134703，2.10 仍受影响，因此选择 2.11。真实探针分别核对系统库与 Python 内置副本 | 系统候选补丁待对应 CI；Python 内置副本仍需单独处理，不因 dpkg 升级就关闭该路径，见[记录](checks/20261125-expat-system.md) |
| ACL：54369 | 特权调用方处理可被替换的 ACL 路径 | 容器实际 UID 10001、无 capabilities、NoNewPrivs 已检查；未完成具体 ACL 利用回放 | 保留来源公告，不根据“没直接 import”判安全 |

以上是从当前源码、部署文件和实际库存得到的入口核对。后续如新增协议、递归克隆、补丁执行、数据库上传、QUIC 或多实例，应重新评估；当前结论不自动适用于新的使用方式。未知级别、没有修复版本和未审完的条目完整保留。

## 证据与限制

- Git 参数和环境：`indexer/cloner.py`、`security/git_transport.py`、`indexer/snapshots.py`，有协议、hooks、凭据、大小与超时回归。
- SQLite schema 和参数：`indexer/sqlite_vectors.py`、`agent/conversations.py`、任务和版本库；备份工具拒绝符号链接，恢复仅到新目录。
- 网络和进程：`deploy/compose.yaml`、`deploy/nginx.conf.template`、`ops/privileged_tools.py`、实际 Linux 烟测。
- [util-linux 78410 官方说明](https://security-tracker.debian.org/tracker/CVE-2026-78410)指出受限 bind mount 的源路径可被替换。
- [SQLite 119374](https://security-tracker.debian.org/tracker/CVE-2026-119374)与[119379](https://security-tracker.debian.org/tracker/CVE-2026-119379)分别描述损坏的 FTS5 索引和 session changeset。
- [OpenSSL 124709 官方说明](https://security-tracker.debian.org/tracker/CVE-2026-124709)描述 QUIC 服务端条件，并将实际影响解释为缓慢的内存增长；保留 scanner 原始 High，不能直接据此删记录。

Git/curl 条件来自此次完整扫描中的说明及上游参考链接；部分官方网页本次读取超时，没有冒充已重新下载成功。原扫描记录绑定镜像与摘要保存，可独立复核。

2026-11-23 补充：重新读取 [Git 官方 111760 公告](https://github.com/git/git/security/advisories/GHSA-vp2w-7xxj-jvxr)，其最新说明比归档扫描描述更广：非递归克隆的检出也可覆盖 `.git`，后续 `git status` 可经 fsmonitor 执行代码。此前把触发条件写成“递归子模块”不完整，现已更正；`--no-checkout` 必须固定，不能只依赖不加递归参数。[Debian 114466](https://security-tracker.debian.org/tracker/CVE-2026-114466)仍标为未修复。本轮不改变镜像、锁、扫描范围、原始公告或发布门槛。

修正后的 `48f6bb1` 实际 Mac/Linux CI 全通过：418 项测试，10 个 APT/特权工具拒绝执行，原生依赖和迁移恢复通过。展开层总大小减少约 51 MiB；同一数据库的旧布局与候选都为 336 条/177 个编号，系统包与版本完全相同。详见[远端回读](checks/20261123-security-readback.md)。后续 `c42042a` 的 422 项和 `28ac3e8` 的 427 项 Mac/Linux 全通过，后者真实解析器拒绝与 Git 依赖、旧数据恢复通过，扫描数未变，见[Git](checks/20261123-git-policy.md)、[解析器](checks/20261123-optional-parsers.md)。SQLite 限制见[独立记录](checks/20261123-sqlite-policy.md)。

本轮不调整 `validate_os_audit` 或 `release_gate`。所有剩余系统命中仍阻断公开部署；真实模型、真实登录、目标主机负载和真实生产恢复也尚未验收。
