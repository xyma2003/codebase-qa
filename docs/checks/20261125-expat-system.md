# 系统 Expat 定向修复

基线为最近真实成功的 `cfe22d164d2de055a92e88d8dd6b8b8c4e7240d7`、[CI 45155499338](https://github.com/xyma2003/codebase-qa/actions/runs/45155499338)，产物 SHA256 `2ad20c66d77ab64548726d13d7e8a3df09a474323482891b92e1fe14c9119041`。沿用同一 Python 3.12.13 镜像、Mac 192/Linux 195 包哈希锁、模型配置、原数据与扫描门槛。

[Debian 当前来源公告](https://security-tracker.debian.org/tracker/source-package/expat)显示：2.10 已修复原 113351/115506/117610/118370，但 124317/128241/134703 仍受影响，2.11 修复这三项。因此本轮选官方 `libexpat1_2.11.0-1_amd64.deb`，不先安装有已知待修复项的 2.10。

[官方下载页](https://packages.debian.org/sid/amd64/libexpat1/download)与实际包均为 142788 字节，SHA256 `2a8e9ee7746f8563cd38b7acb324c4ad61c2c877e7ba5f9a5001f44998876526`；control 字段为 libexpat1、来源 expat、版本 2.11.0-1、amd64、Pre-Depends libc6 >=2.38，现有 Debian 13 libc6 为 2.41。只装这一个包；不启用 sid APT 源、不开自动依赖修复。下载和 control 不符即拒绝，保留原 dpkg 错误，不重试安装。大 Packages.xz 下载曾超时，未拿截断索引当作有效依据，改用小型官方下载页的明确校验值。

构建前后查询全部 dpkg 状态和版本，只接受 libexpat1 的精确变化。CI 对最近成功代码的同底座镜像和候选使用同一 Trivy 数据库，允许的变化也只有该包；扫描记录完整保留，无 VEX 或过滤。此包不在稳定发行版当前修复集合内，后续维护需要继续核对官方更新与依赖，不暗示能随普通 apt upgrade 自动维持新版本。

运行探针核对实际动态加载的系统 XML_ExpatVersion、解析/重置，以及 Python pyexpat 的版本、文件摘要和 ldd。Python 官方镜像可内置另一份 Expat；系统 dpkg 更新不能作为该副本修复的证据。[CPython 3.12.13 的内置头文件](https://github.com/python/cpython/blob/v3.12.13/Modules/expat/expat.h)版本为 2.7.4，必须单独检查和处理。探针不把未修复副本记成成功，也不授权公开发布。

本机 **20 项定向、445 项全量 / 129.718 秒 / 退出 0**；actionlint 和 diff 检查通过。测试覆盖下载哈希/大小/重定向、元数据、架构、基线版本、dpkg 原错误、禁止未声明包变化。实际 Linux 更新、双引擎、迁移恢复与完整新扫描在本提交 CI 独立验收；真实模型请求 0，密钥/OIDC 后置。

`73cd652` 的 Linux [CI 45575017335](https://github.com/xyma2003/codebase-qa/actions/runs/45575017335)在安装后完整性校验处失败，原异常为 `Installed Expat package verification failed`，没有输出具体 audit/verify 内容。尚未把该候选记为成功。下一次仅修正错误输出，保留两个命令的完整结果；包版本、哈希、依赖、基线和安装参数不变，不跳过校验或猜测调整参数。

诊断提交 `6ba254c` 的 Linux [CI 45575508582](https://github.com/xyma2003/codebase-qa/actions/runs/45575508582)确认：audit 为空，verify 原始结果为缺少 `/usr/share/doc/libexpat1/AUTHORS`、`changelog.Debian.gz`、`changelog.gz`。精简镜像的文档排除策略导致该失败。本次仅在安装 libexpat1 时增加 `--path-include=/usr/share/doc/libexpat1/*`，保留该包文档，不改全局 dpkg 配置，继续要求 audit/verify 完全为空。版本、哈希、依赖、基线、下载和扫描参数均保持，不跳过校验。

`83549c4` 的 [CI 45575949037](https://github.com/xyma2003/codebase-qa/actions/runs/45575949037)已全部通过：Mac 446 项/143.844 秒；Linux 包 audit/verify、原生解析与重置、旧数据迁移和恢复通过。Linux 产物 SHA256 `96d81519a71736e2f296ab9a88053205753a407510387b40525d596ea62c8dee` 已下载核对，候选镜像 `sha256:f4c624400a4895be757acc226f3c6d44d60f0bafd7aa7be3fbdbc00a09235e4c`。实际只改变 libexpat1，其他 117 个系统包不变；系统加载 2.11.0，Python 实测仍为内置 2.7.4、ldd 不引用系统 expat，确认需要第二项修复。

同一新 Trivy 数据库的对照均为 **342 条/179 编号/22 个不同 High**，无增减命中；完整发布阻断保留。相对 11 月 23 日旧数据库，新增编号为 OpenSSL 的 107494（High）、84785（Unknown）和 shadow 的 135901（Unknown），同时有旧记录消失，不能把跨日期总数差当作本次补丁的效果。真实系统版本与原始扫描分别保留。
