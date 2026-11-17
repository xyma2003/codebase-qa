# Debian 系统包定向整改 · 2026-11-17

本机 **383 项通过，120.900 秒，进程退出 0**，其中新增 6 项系统包检查测试。[日志](20261117-os-packages-unittest.log)、[定向测试](20261117-os-packages-targeted.log)和[实现指纹](20261117-os-packages.json)分别保存。本机没有 Docker，实际 Linux 安装版本和漏洞清除数量以本提交的 CI 产物为准。

## 基线与修复目标

前一项 Pydantic 整改 `8bf28d3` 的 [Mac/Linux CI](https://github.com/xyma2003/codebase-qa/actions/runs/44049495312) 已通过。Linux 的 196 个运行包与锁一致，193 个公共版本与当前 Mac 锁一致；Python 复查剩 4 组 Chroma 公告。TLS、WebSocket、限流、未配置时拒绝启动及原生数据恢复均通过。

系统包原始基线取自 `2c71603` 的 [CI](https://github.com/xyma2003/codebase-qa/actions/runs/44042931432)：483 条命中、243 个不同公告编号。其中 7 个包的 38 条命中（25 个编号）给出修复版本。Pydantic 提交的复扫仍为 483 条。本轮仅定向更新这 7 个包：

| 包 | 基线版本 | 最低修复版本 |
| --- | --- | --- |
| apt / libapt-pkg6.0 | 2.6.1 | 2.6.2 |
| liblzma5 | 5.4.1-1+deb12u1 | 5.4.1-1+deb12u2 |
| libpcre2-8-0 | 10.42-1 | 10.42-1+deb12u2 |
| libssl3 / openssl | 3.0.20-1~deb12u2 | 3.0.22-1~deb12u2 |
| tzdata | 2026b-0+deb12u1 | 2026f-0+deb12u1 |

版本来自实际扫描，并与 Debian 官方包记录核对：[APT](https://packages.debian.org/bookworm/apt)、[XZ](https://packages.debian.org/bookworm/liblzma5)、[PCRE2](https://packages.debian.org/bookworm/libpcre2-8-0)、[OpenSSL](https://packages.debian.org/bookworm/openssl)、[时区数据](https://packages.debian.org/bookworm/tzdata)。目标清单保存原扫描和下载 ZIP 的 SHA256，不以字符串排序猜测 Debian 版本。

## 检查与阻断

构建时显式更新目标包，保留 Python 3.12.13 和 Debian 12 底座。安装后逐项读取 `dpkg-query` 状态，调用原生 `dpkg --compare-versions` 检查最低版本；缺包、部分安装、版本过低、比较失败或发行版变化都拒绝继续，保留原始错误。实际版本清单保存在镜像内，CI 取回归档。

完整 Trivy 扫描仍执行，不使用忽略列表或 `ignore-unfixed`。新增检查要求扫描覆盖 Debian 12 及目标包库存，原 38 条目标命中必须全部消失。其余条目继续报告，仍有公告就继续阻断公开发布。定向检查不能代替完整 OS/Python 审计。

新增测试覆盖完整安装查询与 Debian 比较调用、比较失败、原始查询错误、缺包/残留配置、错误发行版/清单，以及目标命中未清除和扫描缺失。保留“目标已清除但其他漏洞仍阻断”的检查。原始真实扫描已通过新检查实测被拒绝，不能误记为修复完成。

## 未完成

本机测试验证检查逻辑；实际 Linux 安装、扫描及容器功能由此次提交 CI 验收。剩余无修复版本的系统公告、Chroma 的 4 组公告、真实 OIDC/模型/域名和生产主机验收继续保留。当前没有开放公网，也没有运行真实模型。
