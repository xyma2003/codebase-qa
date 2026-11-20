# 依赖审计 · 2026-11-17

2026-11-20：应用切换 SQLite + NumPy 后，运行锁只移除 `chromadb==1.5.9`，其余版本及哈希保留。Mac 从 193 到 192 包，Linux 从 196 到 195 包；含 Chroma 的旧锁与原始阶段零均另存。192 包 OSV 查询完整、公告为 0，见[原始审计](checks/20261120-no-chroma-audit.json)。Linux 镜像及 195 包完整查询也已通过：0 Python 公告，跨后端迁移与恢复通过；系统扫描 423 条/200 编号，实际发布检查继续拒绝。见[最终回读](checks/20261120-security-readback.md)。

原始阶段零锁保留，新的精确版本和哈希锁放在 `dependencies/`。这次只变更下表版本并移除全量 Pydantic AI 元包，其余 188 个版本不变。兼容检查不能代替漏洞检查。

## 第一项：Pydantic AI

| 包 | 原版本 | 当前版本 | 原因 |
| --- | --- | --- | --- |
| pydantic-ai | 1.91.0 | 移除元包 | 使用官方精简发行包，保留相同的 `pydantic_ai` 导入名 |
| pydantic-ai-slim | 1.91.0 | 1.107.7 | 覆盖原审计中的 11 组 Pydantic AI 公告 |
| pydantic-graph / pydantic-evals | 1.91.0 | 1.107.7 | 保持框架家族精确版本一致 |
| genai-prices | 0.2.2 | 0.0.73 | 修复版明确要求 `>=0.0.62,<0.1` |
| anthropic | 2.7.0 | 0.125.0 | 修复版明确要求 `>=0.108.0,<1` |

两个 SDK 依照框架要求回到兼容版本范围，经过新锁的漏洞复查；没有放宽上游约束。直接依赖仅启用 `[anthropic,openai]`，不激活未使用的 Hugging Face 适配器，原嵌入相关版本保持不变。参考[官方精简安装说明](https://ai.pydantic.dev/install/)及[1.107.7 发布记录](https://github.com/pydantic/pydantic-ai/releases/tag/v1.107.7)。

## 公告与处理安排

下表保留原 15 组公告的处理轨迹；Pydantic AI 各项已通过新锁查询消除，Chroma 四项继续阻断。

| 公告 | 涉及能力 | 原审计时的路径判断 |
| --- | --- | --- |
| [GHSA-36p7-vc44-83pf](https://github.com/advisories/GHSA-36p7-vc44-83pf) | Chroma REST 更新集合时允许远端模型代码 | Critical；本项目使用本地 PersistentClient，没有启动 REST 服务；保留阻断，不能据此宣布依赖已修复 |
| [GHSA-f4j7-r4q5-qw2c](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c) | Chroma REST 创建集合时的未认证代码注入 | Critical；同上，公告没有给出已修复版本 |
| [GHSA-2wm9-hf6c-p5cr](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr) | Chroma 服务端跨租户访问 | High；当前用户隔离由应用实现，未启用该 REST 认证边界；继续阻断 |
| [GHSA-xph7-9rjv-w5fr](https://github.com/advisories/GHSA-xph7-9rjv-w5fr) | Chroma SimpleRBAC 对资源范围检查不足 | High；未启用该服务端组件，继续阻断 |
| [GHSA-cg7w-rg45-pc59](https://github.com/advisories/GHSA-cg7w-rg45-pc59) | Pydantic AI 内置下载的 IPv6 SSRF 绕过 | 与其他下载公告一起整改，不自动放行 |
| [GHSA-cqp8-fcvh-x7r3](https://github.com/advisories/GHSA-cqp8-fcvh-x7r3) | IPv4 映射 IPv6 的元数据访问绕过 | 同上 |
| [GHSA-vmxc-h2x2-jmf3](https://github.com/advisories/GHSA-vmxc-h2x2-jmf3) | IPv6 zone identifier 绕过 | 同上 |
| [GHSA-h7p7-w5gc-xj3w](https://github.com/advisories/GHSA-h7p7-w5gc-xj3w) | UploadedFile 引用验证不足 | 网页仅传问题字符串，未启用该上传适配器；继续阻断 |
| [GHSA-v2xh-2vp8-57h8](https://github.com/advisories/GHSA-v2xh-2vp8-57h8) | 远端内容下载未限制内存 | 没有注册 web_fetch/FileUrl 工具；继续阻断 |
| [GHSA-fpf4-vwcp-v4hp](https://github.com/advisories/GHSA-fpf4-vwcp-v4hp) | web_fetch 标题提取阻塞循环 | 同上 |
| [GHSA-v36g-jcw9-x7cw](https://github.com/advisories/GHSA-v36g-jcw9-x7cw) | 嵌套 HTML 转换资源消耗 | 同上 |
| [GHSA-h4xc-3qfq-jf93](https://github.com/advisories/GHSA-h4xc-3qfq-jf93) | Pydantic AI 自带网页聊天接口被跨站触发 | High；本项目使用 Streamlit，没有启用 to_web/clai web；继续阻断 |
| [GHSA-q2xc-rrxj-58x9](https://github.com/advisories/GHSA-q2xc-rrxj-58x9) | 自带聊天接口未核对 Host | 同上 |
| [GHSA-3gh4-cghq-f8v4](https://github.com/advisories/GHSA-3gh4-cghq-f8v4) | OpenTelemetry 重试内容脱敏不足 | 没有主动启用内容追踪；继续阻断，不用配置推断代替修复 |
| [GHSA-4x9p-g9wm-8q7f](https://github.com/advisories/GHSA-4x9p-g9wm-8q7f) | OpenTelemetry 异常事件可能包含内容 | 同上 |

Chroma 四组公告没有标出 patched version，核对 PyPI 后当前最新仍为 1.5.9。本项目没有启动 REST 服务，但这不能证明依赖已修复；不盲目降级现有数据库，不添加忽略名单。

2026-11-19 重新查询当前 193 个精确版本，仍为 8 个编号/4 组 Chroma 公告。官方稳定发行版仍为 1.5.9，两个代码注入公告的 patched version 仍为空。新增本地客户端配置边界：源码和缓存统一固定 Rust 嵌入式 API，不读取环境、dotenv 或 secrets 组件配置，禁用 reset、匿名遥测和 OTel 导出。保留原依赖、模型、集合和数据格式；这是防护加固，不把公告从审计结果删除。见[检查记录](checks/20261119-chroma-local.md)。

## 可重复检查

```bash
.venv/bin/python scripts/audit_dependencies.py --check-installed \
  --output .baseline_runtime/audit-new-run
```

存在未处理公告时命令返回非零。`--collect-only` 只表示完整收集，用于 CI 保存材料；会明确输出 `release_allowed: false`。它不影响部署入口的严格检查。

审计记录保存锁文件 SHA256、查询时间、完整包列表、分页请求及公告响应。发布检查离线重放响应，不信任手改的 findings 摘要；缺页、接口错误、锁不匹配、记录超过 7 天或仍有公告时拒绝。只向公开 OSV 接口发送包名和版本，不发送代码、问题、密钥或用户身份。

## Linux 与系统包

Linux 直接使用已验证的 CPU 哈希锁，不在构建时重新解析；当前锁仅从最近成功的 196 包锁移除 Chroma 为 195 包。192 个公共版本与 Mac 一致；PyTorch 使用对应 `+cpu` wheel，其公共版本用于 OSV 查询。旧基线镜像使用另存的原 196 包锁，新旧摘要分开检查。实际安装、导入和完整查询由 Linux CI 验证并归档。

原提交 `2c71603` 的 Linux CI 已通过，安装 197 个包并逐项与审计锁一致；Trivy 返回 483 条系统包命中、243 个不同漏洞编号，其中 38 条有修复版本。此数字是整改前记录，不能直接当作当前风险数量或已经修复的结果。

Pydantic 整改提交 `8bf28d3` 的 Linux CI 已通过，193 个公共版本保持一致，运行环境为 196 个包，Python 查询剩 8 个编号/4 组 Chroma 公告；系统扫描仍为 483 条。后续单独定向更新上面 7 个 Debian 包，具体目标、版本下限、验证范围与未处理项见[系统包整改记录](checks/20261117-os-packages.md)。

CI 另外用固定版本 Trivy 0.76.2 对保存的镜像扫描 OS 包，不向扫描器挂 Docker socket，不忽略未修复项。记录与镜像 ID、代码版本和运行锁绑定；部署入口也要求对应的、7 天内的系统扫描结果且无公告。基础镜像、Python 解释器、应用依赖、真实模型与公网验证仍需分别判断，不以单次扫描保证不存在漏洞。

2026-11-19 的最新成功提交 `b4804a0` 已通过 Mac/Linux CI。系统包库存与 11 月 17 日一致，新扫描为 459 条命中/225 个编号，且当前 Debian 12 扫描未给出修复版本；旧 445 条不能继续冒充最新基线。本次以这份记录验证 Debian 13 升级，固定 Linux 196 个依赖和非目标工具镜像，保留旧最小版本清单及完整数据回读检查。实际新系统扫描结果以本提交 CI 为准，不预先声明零漏洞，见[底座整改记录](checks/20261119-trixie.md)。

数据源：[OSV 查询约定](https://google.github.io/osv.dev/post-v1-querybatch/)、[GitHub Advisory Database](https://github.com/advisories)、[Trivy 固定版本](https://github.com/aquasecurity/trivy/releases/tag/v0.76.2)。本轮查询材料见 [审计记录](checks/20261117-dependency-audit.json)。

2026-11-19 最终代码 `cf1c464` 已通过 Mac/Linux CI。实际 Trixie 扫描为 413 条/194 个编号，Critical 2→0、High 仍为 118 条；对照有 48 个编号不再报告、17 个新增报告，剩余记录均未给出当前稳定发行版的修复版本。Python 仍有 4 组 Chroma 公告。全部保留阻断，后续先核对新增 util-linux 高危与留存高危可达性及补丁方案，另行评估 Chroma 后端替换。详见[最终回读及差异](checks/20261119-security-readback.md)。
