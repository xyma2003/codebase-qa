# Codebase Q&A

输入 GitHub 仓库地址，检索代码并提问。分析使用固定的 Git 代码版本，搜索、文件读取和历史问答保持在同一版本。

[产品方案](docs/PRODUCT_PLAN.md) · [技术方案](docs/TECHNICAL_PLAN.md) · [实施进度](docs/IMPLEMENTATION_STATUS.md) · [阶段零记录](docs/baseline/README.md)

## 当前功能

- 导入公开 GitHub 仓库，Python 按函数和类解析，其他支持的文本按窗口分块。
- 使用本地 `all-MiniLM-L6-v2` 嵌入和 ChromaDB 检索代码。
- 通过 Agent 搜索代码、列出文件、按行读取；回答需要配置模型接口。
- 从 Git 已提交内容建立快照，记录完整 Commit SHA 和文件 SHA256；不会把工作区的未提交文件混入快照。
- 更新索引时先构建、回读验证，再发布新版本。失败保留旧索引，旧版本可以重新选择。
- 历史问答按仓库和代码版本保存，避免直接复用其他版本的答案。
- 提供 Streamlit 页面和 MCP 接口。

真实聊天模型问答仍待 SiliconFlow 密钥。环境、索引、MCP、页面及版本管理的验证范围见[实施进度](docs/IMPLEMENTATION_STATUS.md)，尚未报告回答准确率。

## 安装与配置

本机已验证 Python 3.12，项目使用独立虚拟环境：

```bash
git clone https://github.com/xyma2003/codebase-qa.git
cd codebase-qa
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

复现本机原始环境时，使用[阶段零依赖锁](docs/baseline/runs/20261110-stage0/requirements-hashed.txt)和[运行说明](docs/baseline/README.md)。原依赖声明未改动。

项目 `.env` 使用用户已选择的接口：

```dotenv
OPENAI_API_KEY=your-siliconflow-key
OPENAI_API_BASE=https://api.siliconflow.cn/v1
OPENAI_MODEL=Qwen/Qwen3-32B
```

`.env` 被 Git 忽略。原有 Anthropic 接口也保留在 `config.py`；本轮没有切换模型、修改 Prompt 或调整检索、记忆阈值。

```bash
streamlit run app.py
```

首次索引会下载原有的本地嵌入模型。

## 使用

1. 输入 `https://github.com/owner/repo`，点击“克隆并索引”。
2. 导入成功后，在“代码版本”中查看当前 Commit，并可选择已成功索引的旧版本。
3. “更新索引”抓取远端默认分支的新提交。同一提交已索引时直接复用。
4. 配置模型后，可以提问实现位置、执行流程或修改思路，并核对代码行号。

当前导入和刷新针对远端默认分支；选择旧版本使用已索引的完整 SHA。任意分支选择仍在后续范围。

切换仓库或代码版本会清空当前显示的对话，避免把不同代码的讨论混在一起。真正的多轮模型上下文将在后续阶段实现。当前回答仍沿用原实现，完整生成后分段显示。

## 版本与索引

```text
GitHub 仓库 → 获取 Git 提交 → 源码快照和文件清单
                           ↓
                    解析与新索引构建
                           ↓
                    回读验证 → 发布版本
                           ↓
                 同版本检索、源码和问答记录
```

快照保存在 `repos/.snapshots/`，来自 Git 对象而非可变工作区。符号链接和子模块不会展开。文件读取检查路径范围和快照内容，快照被改动时拒绝把它当作原版本读取。这些检查不等同于操作系统沙箱。

向量和版本元数据保存在 `chroma_db/`。每个已完成版本使用独立集合，SQLite 保存可用版本及当前发布位置；构建失败不会先删除旧集合。原有未版本化索引只有在全部代码内容和嵌入配置匹配时，才复用其向量到新集合，原集合保持不变。

历史问答按仓库和 Commit 隔离。保存条件仍是原来的规则评分；这只是答案积累，不会训练模型，也不能证明答案正确。

## 工具

Agent 保留三个工具：

| 工具 | 功能 |
| --- | --- |
| `search_code(query)` | 在所选版本的索引中检索代码 |
| `list_repo_files(pattern="")` | 列出所选版本已索引的文件 |
| `read_file(file_path, start_line=1, end_line=None)` | 返回真实行号；首尾行均包含在内 |

读取保留单次 8000 字符内容上限，可用后续行范围续读。绝对路径、`..`、符号链接、Git 元数据及二进制内容会被拒绝。单行超限时明确标记部分显示。

MCP 另提供版本选择和查询，共五个工具：

| 工具 | 功能 |
| --- | --- |
| `set_repo(repo_url, refresh=False, commit_sha=None)` | 导入仓库、更新默认分支，或选择已索引的完整 SHA |
| `list_repo_versions()` | 列出当前仓库可用版本 |
| `search_code(query)` | 检索所选版本 |
| `list_repo_files(pattern="")` | 列出所选版本文件 |
| `read_file(file_path, start_line=1, end_line=None)` | 读取所选版本源码 |

`refresh=True` 和 `commit_sha` 不能同时使用。选择失败会清空 MCP 当前选择，后续工具不会继续查询另一个仓库。

MCP 客户端配置示例：

```json
{
  "mcpServers": {
    "codebase-qa": {
      "command": "/absolute/path/to/codebase-qa/.venv/bin/python",
      "args": ["/absolute/path/to/codebase-qa/mcp_server.py"]
    }
  }
}
```

## 检查与开发

```bash
.venv/bin/python -m unittest discover -s tests -v
```

[按行读取验收](docs/checks/20261110-source-reader.md)与[版本管理验收](docs/checks/20261110-repository-versions.md)分别保存记录。真实存储检查脚本为 `scripts/check_repository_versions.py`，新运行使用新的 `--output` 文件，避免覆盖已保存的证据。

每个功能点独立验收、提交和 push。阶段零原版检查点是 `3aee328`，按行读取检查点是 `5a6b945`；需要补原版真实模型基线时，应检出原版代码，不把后续实现的结果记作原版结果。

原集合保留，因此当前已有的阶段零索引仍可供旧代码使用。旧代码不会自动识别新增的版本索引格式；回滚代码不会删除这些新数据。
