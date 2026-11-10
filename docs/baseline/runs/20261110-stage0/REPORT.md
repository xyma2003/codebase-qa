# 第 0 阶段运行记录

日期：2026-11-10。状态：环境、索引、工具与页面验证通过，真实模型问答待配置密钥。尚未建立完整端到端成功基线。

## 代码和环境

- 应用源码：`7ed1f8f22e3c1caf1c23b9ca976bf9e86a5feffb`，15 个原应用、配置及依赖文件的内容与该版本一致。
- 本地分支：`codex/stage0-baseline`。本次增加方案、运行脚本、固定问题和记录，README 增加文档入口。
- Python：3.12.13。系统：macOS 26.7 arm64。使用项目独立 `.venv`。
- 依赖：194 个，实际安装版本与官方 PyPI 解析的锁文件完全一致，`uv pip check` 通过。
- 用户选择：SiliconFlow `Qwen/Qwen3-32B`，地址 `https://api.siliconflow.cn/v1`。当前没有 API Key，没有发送模型请求。

完整脱敏配置和源码校验值见 [inspect.json](inspect.json)，实际依赖版本见 [requirements-resolved.txt](requirements-resolved.txt)，包含官方 SHA256 的锁文件见 [requirements-hashed.txt](requirements-hashed.txt)。

原程序在 SiliconFlow 密钥为空时选择 Anthropic 分支。这是原代码的有效状态，未改动；问答复查脚本在发送请求前检查用户所选接口，当前直接记录待配置，未调用该分支。

## 固定输入

目标仓库是 `https://github.com/xyma2003/codebase-qa`，实际克隆 SHA 与上述版本一致。嵌入模型保持 `all-MiniLM-L6-v2`，实际下载 revision 为 `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`，文件 SHA256 见 [embedding-files.json](embedding-files.json)。

本次检索和待执行的真实问答使用 B01：

> 这个项目如何从 GitHub 仓库地址建立索引？请指出主要函数、文件和执行顺序。

完整 6 个固定问题及人工核对点见 [questions.json](../../questions.json)。核对点不进入模型提示词。检索 Top K 为原实现的 5，分块及记忆阈值均保留原值。

## 运行结果

| 检查 | 实际结果 | 记录 |
| --- | --- | --- |
| 环境与源码检查 | 通过，194 个包与官方版本锁一致，原应用文件未改 | [inspect.json](inspect.json) |
| 克隆、解析、嵌入与索引 | 通过，16 个文件、58 个代码块 | [index.json](index.json) |
| 持久化回读 | 58 个身份及文本匹配，代码块行号匹配 | [index.json](index.json) |
| 语义检索 | 返回 5 个非空代码片段；相关性尚未人工评分 | [retrieval-results.json](retrieval-results.json) |
| MCP stdio 协议 | 通过，协议版本 2025-11-25，4 个工具调用成功 | [mcp.json](mcp.json)、[mcp-results.json](mcp-results.json) |
| Streamlit AppTest | 首次渲染、输入已有仓库后的渲染均通过 | [ui.json](ui.json) |
| 浏览器 | 实际显示 Codebase Q&A、所选仓库和“已索引 ✓” | [browser.json](browser.json)、[完整截图](browser.png) |
| 本地健康接口 | 返回 `ok`，服务只监听 `127.0.0.1:18524` | [browser.json](browser.json) |
| 原版真实模型问答 | 待密钥，未提交问题，无模型回答或准确率结果 | [ask.json](ask.json) |

索引阶段耗时 88.139 秒，包含首次导入及模型准备，不能当成纯索引算法耗时。MCP 检查耗时 8.207 秒，页面 AppTest 耗时 1.399 秒。

MCP 调用依次为 `set_repo`、`list_repo_files`、`read_file` 和 `search_code`。已有索引未重复构建；历史问答库仍为空，没有写入模拟答案。

## 安装过程

原 `requirements.txt` 不变。官方源首次解析出 194 个包；实测官方 wheel 下载约 26 KB/s，30 秒后未下载完成。停止该慢速安装后，使用已缓存的官方元数据离线冻结精确版本及 SHA256。

镜像单独安装明确失败：没有 `openai==3.49.0`。随后优先镜像、缺失版本回官方源，在同一锁文件及 `--require-hashes` 校验下完成安装。实际包版本逐项比对一致，没有降级或删除依赖。

安装记录和原始输出见 [installation.json](installation.json)、[首次安装日志](dependency-install.log)、[镜像缺失版本日志](dependency-install-mirror.log)及[最终安装日志](dependency-install-mixed.log)。下载路由变化不涉及模型、Prompt、检索或记忆参数。

## 接着完成第 0 阶段

1. 将 SiliconFlow API Key 填入被 Git 忽略的项目 `.env`，不把密钥发到聊天或写入文档。
2. 先重新记录脱敏配置，确认实际生效的是用户选择的 provider、base URL 和模型。
3. 沿用同一目标代码、B01 问题和其他原有设置，执行 `ask` 阶段；保留本次待配置记录与新结果。
4. 检查真实回答和工具证据，成功后再把完整端到端基线标为完成。

如果出现新错误，先保留原始错误和有效配置，再决定下一步。本阶段未进入版本快照或研究功能开发。
