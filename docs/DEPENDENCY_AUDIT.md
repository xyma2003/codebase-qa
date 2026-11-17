# 依赖审计 · 2026-11-17

已对当前环境的 194 个精确版本完整查询 OSV，并逐项取回公告。返回 41 个公告编号，按 CVE/GHSA/PYSEC 别名合并后为 **15 组**，涉及 `pydantic-ai==1.91.0`、`pydantic-ai-slim==1.91.0` 和 `chromadb==1.5.9`。查询完成不等于没有漏洞；当前发布检查保持阻断。

原始阶段零锁和本机环境均保留，没有批量升级。已安装版本与原锁逐项一致，194 个包兼容检查通过。这仅说明安装关系一致，不能代替漏洞检查。

## 公告与处理安排

| 公告 | 涉及能力 | 当前处理 |
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

Pydantic AI 这些公告列出的 1.x 修复版本最高到 `1.107.7`，可作为下一项兼容性验证的起点。Chroma 四组公告没有标出 patched version，需要核对维护方方案或进一步隔离，不建议盲目降级旧数据库。上述未启用路径属于代码及配置范围判断，不是漏洞利用验证，也没有添加忽略名单。

## 可重复检查

```bash
.venv/bin/python scripts/audit_dependencies.py --check-installed \
  --output .baseline_runtime/audit-new-run
```

存在未处理公告时命令返回非零。`--collect-only` 只表示完整收集，用于 CI 保存材料；会明确输出 `release_allowed: false`。它不影响部署入口的严格检查。

审计记录保存锁文件 SHA256、查询时间、完整包列表、分页请求及公告响应。发布检查离线重放响应，不信任手改的 findings 摘要；缺页、接口错误、锁不匹配、记录超过 7 天或仍有公告时拒绝。只向公开 OSV 接口发送包名和版本，不发送代码、问题、密钥或用户身份。

## Linux 与系统包

Linux 使用从同一组精确版本解析的独立 CPU 锁，不覆盖 Mac 原锁。公共版本必须逐项一致；PyTorch 允许对应 `+cpu` wheel，其公共版本用于 OSV 查询。本机 PyPI 解析超时及 Docker 注册表连接失败的原始错误保留，没有把失败写成通过。实际解析、安装和导入由 Linux CI 验证并归档锁及镜像摘要。

CI 另外用固定版本 Trivy 0.76.2 对保存的镜像扫描 OS 包，不向扫描器挂 Docker socket，不忽略未修复项。记录与镜像 ID、代码版本和运行锁绑定；部署入口也要求对应的、7 天内的系统扫描结果且无公告。基础镜像、Python 解释器、应用依赖、真实模型与公网验证仍需分别判断，不以单次扫描保证不存在漏洞。

数据源：[OSV 查询约定](https://google.github.io/osv.dev/post-v1-querybatch/)、[GitHub Advisory Database](https://github.com/advisories)、[Trivy 固定版本](https://github.com/aquasecurity/trivy/releases/tag/v0.76.2)。本轮查询材料见 [审计记录](checks/20261117-dependency-audit.json)。
