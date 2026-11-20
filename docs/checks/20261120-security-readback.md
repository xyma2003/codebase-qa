# 七组安全问题的整改回读 · 2026-11-20

本轮分别提交 util-linux 特权入口隔离 `b2d98ab`、SQLite 向量替换 `05d11fe`、Chroma 依赖移除 `e0d11f3`。CI 缓存收尾修正 `6408ffc` 的 [Mac/Linux CI](https://github.com/xyma2003/codebase-qa/actions/runs/44694064213) 全部通过：Mac 408 项、152.553 秒；Linux 实际 195 包完整查询 0 公告，容器、跨后端迁移、恢复和系统扫描通过。真实模型、OIDC 与公网验收按约定未执行。下面明确区分移除漏洞组件和限制可调用入口。

## 逐组状态

| 问题 | 本轮处理 | 状态与边界 |
| --- | --- | --- |
| CVE-2026-76642：挂载辅助程序失败后仍执行特权 hook | mount/umount 权限 000，去掉 Set-ID，运行时 capability 全空、NoNewPrivs=1 | 调用隔离通过；系统源码未修复，公告仍保留 |
| CVE-2026-78408：nsenter 继承特权 cgroup 文件描述符 | nsenter 权限 000；实际执行被拒绝，不挂载宿主 Docker socket 或特权运维目录 | 调用隔离通过；不覆盖宿主机特权运维操作 |
| CVE-2026-78409：子目录挂载可越过路径范围 | mount/unshare 权限 000；实际 unshare(CLONE_NEWNS) 返回 EPERM | 调用隔离通过；libmount 仍存在，公告不豁免 |
| GHSA-36p7-vc44-83pf / CVE-2026-45833：已认证动态嵌入代码执行 | SQLite + NumPy 替换；移除 Chroma 包；配置不加载组件，嵌入由程序固定且不信任远程代码 | 当前 Python 运行依赖中不再命中 |
| GHSA-f4j7-r4q5-qw2c / CVE-2026-45829：认证前动态嵌入代码执行 | 同上，无 Chroma REST 创建集合入口；新环境断言包不可导入 | 当前 Python 运行依赖中不再命中 |
| GHSA-2wm9-hf6c-p5cr / CVE-2026-45830：跨租户集合读写 | 移除 Chroma 服务端组件；保留应用身份、私有缓存命名空间与逐次所有者检查 | 当前组件移除，SQLite 上的双用户隔离与攻击回归通过 |
| GHSA-xph7-9rjv-w5fr / CVE-2026-45831：RBAC 未检查具体资源范围 | 移除 Chroma RBAC；应用服务端权限检查不依赖该组件 | 当前组件移除，未自动给管理员他人数据权限 |

前三项官方 Trixie 稳定包仍标为 vulnerable/no-dsa：[76642](https://security-tracker.debian.org/tracker/CVE-2026-76642)、[78408](https://security-tracker.debian.org/tracker/CVE-2026-78408)、[78409](https://security-tracker.debian.org/tracker/CVE-2026-78409)。因此没有把它们关闭为“已打补丁”，没有混装 unstable、改 scanner 严重级别、添加 VEX/忽略名单或隐藏扫描结果。

Chroma 四组原公告：[动态配置执行](https://github.com/advisories/GHSA-36p7-vc44-83pf)、[认证前执行](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c)、[跨租户集合](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr)、[RBAC 范围](https://github.com/advisories/GHSA-xph7-9rjv-w5fr)。本轮是从运行组件移除问题来源，不是冒充上游修复；原环境含漏洞，仅作为停止服务后的隔离迁移与回滚资料。

## 数据与验证

- 本机 4 集合 / 1256 条原向量、文本、元数据和配置逐条一致；没有重算嵌入。旧 Chroma 文件与副本保留，新库单独原子发布。
- 固定 19 问 × 4 集合的 76 次 top-5 及顺序全部一致，嵌入误差 0，距离最大误差 3.424e-7；不把这等同于普遍性能或真实回答质量提升。
- 活动版本仍 `7ed1f8f` / 58 分块，原 2 会话 / 3 轮摘要不变；阶段零两份原锁摘要不变。
- Mac 无 Chroma 的 192 包环境全量 408 项通过，120.651 秒；MCP 9 工具 / 14 调用和独立进程恢复通过。存储替换前后的完整记录与锁差异独立保存。
- Python 发布检查实际通过，OS 发布检查实际以“系统依赖仍有未处理公告，禁止上线”拒绝；不是依靠缺少密钥来阻止当前漏洞环境上线。
- Linux 各提交实际结果、原始扫描和下载摘要以[结构化回读](20261120-security-readback.json)为准。

## 发布边界

首次和最终复扫均为系统 423 条 / 200 个编号，0 Critical，扫描没有提供剩余修复版本。相对上一轮 413 条 / 194 编号多 10 条 / 6 个编号，包库存逐字节相同：是公告库变化，不能记作代码整改引入了新包，也不能继续引用旧的 413。

新增编号为 CVE-2026-126765、127039、127040、127231、132138、84786，涉及 glibc、GnuTLS、SASL 和 OpenSSL，当前为 UNKNOWN 或 MEDIUM，完整原始记录不丢弃。最终扫描与编号列表见结构化回读。

**公开发布继续阻断。** Chroma 四组通过替换运行组件处理；util-linux 三组只完成调用隔离，系统源码修复和其余系统公告仍需处理。下一步按官方稳定包更新与实际可达性逐项核对，不把隔离措施当成整体安全验收，也不只凭 Python 零命中宣布可上线。密钥相关验收仍单独后置。

首次依赖提交的 Linux 所有应用与安全检查通过后，setup-uv 保存不存在的宿主缓存失败；没有直接重跑，单独关闭不需要的 Linux 宿主缓存后 CI 全通过。原错误、作业编号与失败归档保留，见[缓存修正记录](20261120-ci-cache.md)。
