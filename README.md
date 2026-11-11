# Codebase Q&A

输入 GitHub 仓库地址，检索代码并提问。分析使用固定的 Git 代码版本，搜索、文件读取和历史问答保持在同一版本。

[产品方案](docs/PRODUCT_PLAN.md) · [技术方案](docs/TECHNICAL_PLAN.md) · [实施进度](docs/IMPLEMENTATION_STATUS.md) · [阶段零记录](docs/baseline/README.md)

## 当前功能

- 导入公开 GitHub 仓库，Python 按函数和类解析，其他支持的文本按窗口分块。
- 使用本地 `all-MiniLM-L6-v2` 嵌入和 ChromaDB 检索代码。
- 通过 Agent 搜索代码、列出文件、按行读取；回答需要配置模型接口。
- 按文本、路径和 Python 函数或类名查找当前快照，覆盖导入语句和模块配置；支持路径过滤、大小写选择和分页。
- 查看静态项目概览：目录文件数、Python 模块定义与导入、配置文件、README 摘录，以及可回到原文的入口线索。
- 通过 Agent/MCP 双向定位 Python 导入候选，保留相对导入、别名、条件/函数上下文及未确定项。
- 从 Git 已提交内容建立快照，记录完整 Commit SHA 和文件 SHA256；不会把工作区的未提交文件混入快照。
- 更新索引时先构建、回读验证，再发布新版本。失败保留旧索引，旧版本可以重新选择。
- 历史问答按仓库和代码版本保存，避免直接复用其他版本的答案。
- 连续追问会传入前面的原生消息，包含问题、回答和工具结果；对话绑定仓库、Commit 和索引，可点击“新建对话”重新开始。
- 选择“修改规划”，输出当前实现、涉及文件、修改顺序、验证方法和待确认项，并检查源码引用、文件路径及步骤覆盖。
- 版本化问答使用本次工具读取生成的引用编号，核对原文后生成固定 Commit 的源码链接；页面可展开查看行号和原文。
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

`.env` 被 Git 忽略。原有 Anthropic 接口也保留在 `config.py`。模型和检索、记忆阈值保持原值；版本化问答新增引用格式指令，要求使用工具提供的编号。

```bash
streamlit run app.py
```

首次索引会下载原有的本地嵌入模型。

## 使用

1. 输入 `https://github.com/owner/repo`，点击“克隆并索引”。
2. 导入成功后，在“代码版本”中查看当前 Commit，并可选择已成功索引的旧版本。
3. “更新索引”抓取远端默认分支的新提交。同一提交已索引时直接复用。
4. 配置模型后，可以提问实现位置、执行流程或修改思路，并核对代码行号。
5. 需要修改方案时，在“任务”中选择“修改规划”，输入需求；切回“问答”可以继续追问同一方案。

导入后可点击“查看项目概览”，不需要聊天模型密钥。概览展示选定快照中的事实：目录、Python 模块、配置文件、README 原文，以及 Python 顶层启动判断、`__main__.py` 和 Streamlit 页面配置调用。入口线索可展开查看源码，也可打开固定 Commit 的链接；它们不证明对应程序已成功运行。用途与模块职责的分析说明仍需模型及后续验证。

概览文件数来自排除依赖、构建目录后的快照清单，包含未进入向量索引的文件，因此可能与“已索引文件数”不同。切换仓库或版本会清除旧概览。

当前导入和刷新针对远端默认分支；选择旧版本使用已索引的完整 SHA。任意分支选择仍在后续范围。

连续追问会使用同一会话前面的模型消息，工具调用和返回结果也会传入。切换仓库、代码版本或索引集合，以及点击“新建对话”，会清空当前显示的对话并开始独立上下文。请求失败保留已完成轮次，不自动重试。

对话原生消息和已核对的回答保存在 `chroma_db/conversations.sqlite3`，每个完整轮次一次写入。同一对话内，工具发出的引用编号连续递增；追问仍需重新读取源码，旧编号不会自动成为本轮证据。追问不写入独立问答缓存，原质量评分和阈值不变。

本版保存完整上下文，尚未压缩长对话，也未提供历史会话列表。页面内部重绘保留当前会话；关闭页面或浏览器重新加载后的历史选择留待后续。Python 调用方可用 `create_conversation` 创建编号，并在 `ask` / `ask_stream` 传入 `conversation_id` 继续已保存的会话。MCP 客户端仍自行管理模型对话。当前回答完整生成后分段显示。

修改规划使用结构化输出。修改或删除已有文件必须引用本文件的源码；新增文件引用已有接入位置，路径不能与快照已有内容冲突。规划会检查实施步骤是否覆盖列出的文件，问题显示在“规划检查”中。文件和引用核对不代表方案逻辑正确，验证命令是待执行建议。

Python 接口传入 `mode="plan"` 生成规划，默认 `mode="qa"` 保持普通问答。`ask_stream` 的 `on_result` 回调可取得 `CheckedAnswer.plan`；规划随会话轮次保存，不写入独立问答缓存。生成规划不会改动目标文件或执行命令，结构化格式不合法时直接报错，不自动重试。MCP 工具仍供外部客户端自行组织规划流程。

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

版本化问答另外要求有可核对的引用，且引用没有检查错误，才允许保存。工具为实际读到的完整源码行生成 `[S1]` 等编号；回答生成后再次检查快照、文件校验值、行号和原文。未知编号、缺少引用或源码被改动时，页面会在“源码引用”中列出问题。缓存中的旧回答仅作参考，必须重新读取源码才能获得本次编号。

检查只覆盖这些引用编号的位置和原文，不证明回答结论正确，也不把正文中自由书写的文件名或其他链接自动算作已核对引用。

## 工具

Agent 提供六个工具：

| 工具 | 功能 |
| --- | --- |
| `get_file_dependencies(file_path, direction="outgoing", source_root="", limit=20, offset=0)` | 查导入候选或可能引用该文件的导入位置 |
| `get_repo_overview()` | 查看当前快照的静态目录、Python 模块、README 和入口线索 |
| `find_code(query, mode="text", path_filter="", case_sensitive=False, limit=20, offset=0)` | 按文本、路径或 Python 定义定位当前快照 |
| `search_code(query)` | 在所选版本的索引中检索代码 |
| `list_repo_files(pattern="")` | 列出所选版本已索引的文件 |
| `read_file(file_path, start_line=1, end_line=None)` | 返回真实行号；首尾行均包含在内 |

读取保留单次 8000 字符内容上限，可用后续行范围续读。绝对路径、`..`、符号链接、Git 元数据及二进制内容会被拒绝。单行超限时明确标记部分显示。

`find_code` 的三种模式：

| 模式 | 查找范围与示例 |
| --- | --- |
| `text` | 逐行查找字面文本，如 `OPENAI_MODEL =`、`os.getenv(`；每行只返回一条 |
| `path` | 按文件路径子串定位，如 `config.py`、`indexer/` |
| `symbol` | Python 函数、异步函数及类定义，支持 `Class.method` 和嵌套定义名 |

`query` 和 `path_filter` 都是字面子串，不解释正则或通配符。默认忽略大小写；`limit` 为 1–50，`offset` 从 0 开始，按 `next_offset` 继续分页。结果绑定 Commit，文本和符号查找会核对文件 SHA256；路径模式读取快照文件清单。查找预览是定位线索，仍需 `read_file` 获取可核对的引用编号。

查找仅处理支持的扩展名，跳过依赖、构建等目录。单个文件超过 1 MiB、含二进制内容，或 Python 定义无法解析时会计入 `skipped_count`；`skipped_examples` 最多列出 10 个例子。单次最多检查 2000 个候选文件、读取 10 MiB 源码，触及上限会返回 `scan_limited=True`，可缩小 `path_filter` 后查找。预览最多 240 字符，截短时返回 `preview_truncated=True`。

`get_file_dependencies` 的 `outgoing` 返回文件中的导入引用，`incoming` 返回可能指向该文件的引用。`file_path` 始终是仓库相对路径；`source_root` 默认仓库根目录，`src` 布局需要明确传入 `source_root="src"`。查找不会读取当前解释器的 `sys.path` 或执行目标模块。

每个导入名称或识别到的动态调用计一条引用。结果提供候选文件、源码行号和原文；属性/子模块歧义、通配符、重导出成员及动态加载保留为未确定信息，不当作完整运行关系。`unresolved_count` 统计当前检查范围内的未完整定位引用，不是目标文件的缺失依赖数。`module_not_located` 只表示未在指定源目录找到支持的 `.py` 文件。

条数为 1–50，按 `next_offset` 分页。扫描沿用单文件 1 MiB、总源码 10 MiB、2000 个候选文件的上限，跳过与受限状态单独返回。单条自动原文最多 1000 字符，超出时 `evidence_unavailable=True`，可再用 `read_file` 定位需要的行。Agent 仍需读取取得本次引用编号。

MCP 另提供版本选择、查询和原文校验，共九个工具：

| 工具 | 功能 |
| --- | --- |
| `set_repo(repo_url, refresh=False, commit_sha=None)` | 导入仓库、更新默认分支，或选择已索引的完整 SHA |
| `list_repo_versions()` | 列出当前仓库可用版本 |
| `get_file_dependencies(file_path, direction="outgoing", source_root="", limit=20, offset=0)` | 与 Agent 共用导入候选定位，返回原文及固定版本链接 |
| `get_repo_overview()` | 返回静态概览及入口的源码原文、行号和固定版本链接 |
| `find_code(query, mode="text", path_filter="", case_sensitive=False, limit=20, offset=0)` | 与 Agent 共用快照查找，返回定位结果及扫描范围信息 |
| `search_code(query)` | 检索所选版本 |
| `list_repo_files(pattern="")` | 列出所选版本文件 |
| `read_file(file_path, start_line=1, end_line=None)` | 读取所选版本源码 |
| `verify_source_citation(file_path, start_line, end_line, source_text, commit_sha)` | 核对指定完整 SHA、行号及逐行原文，返回固定版本链接 |

`refresh=True` 和 `commit_sha` 不能同时使用。选择失败会清空 MCP 当前选择，后续工具不会继续查询另一个仓库。

`verify_source_citation` 的 `source_text` 使用不带行号的原文，不包含范围之外的结尾换行。范围首尾均包含；越界、超过 8000 字符读取上限或原文不一致时拒绝通过，不模糊匹配。

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

[源码引用验收](docs/checks/20261110-citations.md)记录本地框架工具调用、MCP、引用检查和页面样例。`scripts/check_citations.py` 不调用真实聊天模型，新运行也必须使用新的 `--output` 文件。

[源码查找验收](docs/checks/20261110-code-search.md)记录 70 项测试、真实快照查找、MCP 和本地模拟模型流程。`scripts/check_code_search.py` 使用已保存的快照，不更新索引；复查时指定新的 `--output` 文件。

[静态概览验收](docs/checks/20261110-overview.md)记录 87 项测试、真实快照事实、MCP 和浏览器检查。`scripts/check_overview.py` 不调用真实模型、不更新索引，复查也需指定新输出文件。

[跨文件依赖验收](docs/checks/20261110-dependencies.md)记录 100 项测试、双向定位、MCP 和本地模拟模型流程。`scripts/check_dependencies.py` 不更新索引；复查时指定新的 `--output` 文件。

每个功能点验收后独立提交，直接 push 到 `main`，保留逐项回滚的提交记录。阶段零原版检查点是 `3aee328`，按行读取检查点是 `5a6b945`；需要补原版真实模型基线时，应检出原版代码，不把后续实现的结果记作原版结果。

原集合保留，因此当前已有的阶段零索引仍可供旧代码使用。旧代码不会自动识别新增的版本索引格式；回滚代码不会删除这些新数据。
