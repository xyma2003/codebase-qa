# 构建与运行镜像精简 · 2026-11-23

最新成功基线为 `aad406e`，[Mac/Linux CI](https://github.com/xyma2003/codebase-qa/actions/runs/45144479377) 已通过。Python 仍为 Mac 192 / Linux 195 包、0 公告；系统稳定更新后扫描为 336 条 / 177 编号、30 组，0 Critical。此前 423 是 11 月 20 日记录，下降不能计为归类工具修复了漏洞。

本项把 uv 和 Python 依赖安装放在构建阶段，固定 Python 镜像摘要与 Linux 哈希锁。在 `/opt/venv` 安装原 195 个版本；运行阶段只复制虚拟环境，移除全局 pip、apt 与 libapt-pkg7.0。没有 autoremove、强拆 Essential 或移除 Git/Perl/TLS 依赖。不能为了降低扫描数量破坏发行包依赖关系。

原 9 个系统包最低版本在构建时完整检查。移除安装工具后，运行期检查其余 7 个包下限、apt/libapt 不存在；完整扫描也要求这两个包不出现。原 40 个目标公告回归保留，不能算 40 个新增修复。完整扫描与发布门槛不变。

Linux CI 实际检查：工具不可找到、无可导入的 pip 或 Chroma、运行用户不能改写依赖、195 包库存逐项一致；导入 Sentence Transformers/tokenizers，执行真实 NumPy/PyTorch CPU 运算，检查 Python、Git HTTPS 辅助程序及 Torch 原生库没有缺失链接。此项不冒充已经验证预训练模型效果。随后复用 TLS、WebSocket、特权工具隔离、跨后端迁移与恢复检查。

增加受控旧布局镜像：基线与候选使用同一 Python 摘要、同一 Linux 锁、UV 和扫描器；在当前稳定源完成更新。先扫描旧布局，再使用同一 Trivy 数据库扫描候选，前后检查数据库 SHA256 没变。系统包对照只允许 apt/libapt 两项移除，任何其他包或版本变化都会失败；全部新旧命中保留，不将统计减少等同于整体安全。

本机全量 420 项通过，114.364 秒；actionlint 和差异检查通过。当前没有本机 Docker，实际构建与安全结果由本提交 CI 回读，见[本机记录](20261123-runtime-layout.json)。模型和密钥未配置、未调用，公开发布仍阻断。
