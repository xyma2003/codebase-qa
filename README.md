# Codebase Q&A

用自然语言提问任意 GitHub 代码仓库。输入仓库地址，点击索引，然后直接问问题——系统会自动检索相关代码片段，交由 Claude 给出精确、带引用路径的回答。

---

## 1. 项目介绍

Codebase Q&A 是一个本地运行的代码问答工具。它将 GitHub 仓库的源代码拆解成结构化代码块、嵌入向量数据库，然后用 AI Agent 回答开发者关于代码的任何问题：函数实现、架构设计、如何添加新功能、某个 bug 在哪里，等等。

**架构总览**

```
┌─────────────────────────────────────────────────────────────────┐
│                        Streamlit UI (app.py)                    │
│                                                                 │
│   ┌──────────────────┐           ┌──────────────────────────┐  │
│   │   侧边栏：仓库索引  │           │   主区域：对话界面          │  │
│   │  repo_url 输入框  │           │   流式输出 Agent 回复       │  │
│   └────────┬─────────┘           └────────────┬─────────────┘  │
└────────────│────────────────────────────────────│───────────────┘
             │ 索引流程                           │ 问答流程
             ▼                                   ▼
┌────────────────────────┐         ┌─────────────────────────────┐
│   indexer 模块          │         │   agent 模块                 │
│                        │         │                             │
│  cloner.py             │         │  qa_agent.py                │
│  ├── clone_repo()      │         │  ├── Agent (Pydantic AI)    │
│  └── remove_repo()     │         │  ├── ask()                  │
│                        │         │  └── ask_stream()           │
│  chunker.py            │         │                             │
│  ├── _parse_python()   │         │  tools.py                   │
│  │   └── AST 解析       │         │  ├── search_code            │
│  ├── _sliding_window() │         │  ├── list_repo_files         │
│  └── chunk_repo()      │         │  └── read_file              │
│                        │         └──────────────┬──────────────┘
│  vector_store.py       │                        │ 调用
│  ├── index_chunks()    │◄── 写入   ┌─────────────▼─────────────┐
│  ├── search()          │──── 查询 ►│   ChromaDB (本地持久化)     │
│  ├── list_files()      │           │   all-MiniLM-L6-v2 嵌入    │
│  └── is_indexed()      │           └───────────────────────────┘
└────────────────────────┘
             │                                   │
             ▼ clone                             ▼ LLM 推理
    ┌─────────────────┐              ┌───────────────────────┐
    │  repos/ 目录     │              │  Anthropic API         │
    │  (浅克隆，depth=1)│              │  claude-sonnet-4-5    │
    └─────────────────┘              └───────────────────────┘
```

---

## 2. 技术栈

| 组件 | 版本 / 说明 |
|------|-------------|
| **Pydantic AI** | `pydantic-ai[anthropic]==1.91.0`，Agent 框架，负责工具注册和流式调用 |
| **Claude Sonnet** | `claude-sonnet-4-5`，通过 Anthropic API 驱动问答推理 |
| **ChromaDB** | 本地持久化向量数据库，存储代码块嵌入，支持语义检索 |
| **sentence-transformers** | `all-MiniLM-L6-v2` 模型，生成代码嵌入向量，完全本地运行 |
| **Streamlit** | Web UI，提供仓库索引侧边栏和流式对话主界面 |
| **GitPython** | 执行 `depth=1` 浅克隆，快速拉取仓库 |
| **python-dotenv** | 从 `.env` 文件加载 API Key |
| **nest-asyncio** | 解决 Streamlit 事件循环与 `asyncio.run()` 的嵌套冲突 |

---

## 3. 工作原理

完整的数据流分为两个阶段：**索引阶段**（一次性）和**问答阶段**（每次提问）。

### 索引阶段

```
GitHub URL
    │
    ▼
clone_repo()          # GitPython depth=1 浅克隆到 repos/owner__reponame/
    │
    ▼
chunk_repo()          # 遍历所有代码文件
    ├── .py 文件 → _parse_python()
    │       ├── ast.parse() 提取 FunctionDef / AsyncFunctionDef / ClassDef
    │       ├── 每个函数/类 = 一个 CodeChunk（含精确行号）
    │       └── 无函数/类或 SyntaxError → 退回 _sliding_window()
    └── 其他扩展名 → _sliding_window()
            └── 按 MAX_CHUNK_TOKENS=512 (≈2048 chars) 切分
    │
    ▼
index_chunks()        # 写入 ChromaDB
    ├── 集合名 = "{owner}-{reponame}"（小写，最长 63 字符）
    ├── 每批 500 条批量写入（避免 SQLite 限制）
    └── 重新索引时先删除旧集合再创建
```

### 问答阶段

```
用户输入问题
    │
    ▼
ask_stream()          # Pydantic AI Agent.run_stream()
    │
    ├── Agent 决策：调用哪些工具
    │       ├── search_code(query)      → ChromaDB 语义检索，返回 top-5 代码片段
    │       ├── list_repo_files(pattern) → 列出所有已索引文件路径
    │       └── read_file(file_path)    → 读取完整文件（超 8000 字符截断）
    │
    ▼
Claude claude-sonnet-4-5   # 综合工具结果生成回答
    │
    ▼
流式 token 输出 → Streamlit 实时渲染
```

---

## 4. 快速开始

### 前提条件

- Python 3.10 或更高版本
- `git` 命令（用于克隆目标仓库）
- Anthropic API Key（[申请地址](https://console.anthropic.com/)）

### 安装步骤

```bash
# 1. 克隆本项目
git clone https://github.com/your-username/codebase-qa.git
cd codebase-qa

# 2. 创建虚拟环境（推荐）
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt
```

> 首次运行时，`sentence-transformers` 会自动从 Hugging Face 下载 `all-MiniLM-L6-v2` 模型（约 90MB），请确保网络畅通。

### 配置

在项目根目录创建 `.env` 文件：

```bash
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxx
```

所有其他配置项在 `config.py` 中，可按需修改：

```python
CLAUDE_MODEL    = "claude-sonnet-4-5"   # 使用的模型
CHROMA_DIR      = "chroma_db"           # 向量数据库存储目录
CLONE_DIR       = "repos"              # 仓库克隆目录
EMBED_MODEL     = "all-MiniLM-L6-v2"   # 嵌入模型
MAX_CHUNK_TOKENS = 512                  # 滑动窗口单块最大 token 数
```

### 启动

```bash
streamlit run app.py
```

浏览器会自动打开 `http://localhost:8501`。

---

## 5. 使用方法

### 索引仓库

1. 在左侧侧边栏的"GitHub 仓库地址"输入框中填入仓库 URL，例如：
   ```
   https://github.com/tiangolo/fastapi
   ```
2. 点击"克隆并索引"按钮。
3. 进度条显示两个阶段：代码解析和向量写入。
4. 完成后显示"已索引 ✓"和总代码块数。

已索引的仓库下次打开时会自动检测，无需重新索引。如需重新索引（例如仓库有更新），点击"重新索引"按钮。

### 提问

索引完成后，在底部输入框直接用自然语言提问，按 Enter 发送。系统支持多轮对话，历史消息会保留在会话中。

### 示例问题

```
这个项目的整体架构是什么？入口在哪里？

用户认证是如何实现的？

search_code 和 list_repo_files 两个函数的区别是什么？

如果我想添加一个新的 API 端点，应该修改哪些文件？

找出所有带 async 关键字的函数，它们分别做什么？

config.py 里有哪些可配置项？
```

---

## 6. 三个工具说明

Agent 拥有三个工具，会根据问题类型自主决定调用顺序和组合方式。

### `search_code`

```python
async def search_code(ctx: RunContext[RepoDeps], query: str) -> str
```

对已索引的代码库执行**语义检索**，返回与 `query` 最相关的 5 个代码片段，每条结果包含文件路径、起止行号、块类型（function/class/other）和代码块名称。

- 适用场景：查找某个函数/类的实现、寻找处理特定逻辑的代码
- Agent 策略：几乎每次提问都会优先调用此工具

### `list_repo_files`

```python
async def list_repo_files(ctx: RunContext[RepoDeps], pattern: str = "") -> str
```

列出已索引仓库中的**所有文件路径**。支持通过 `pattern` 参数按子字符串过滤，例如传入 `"test"` 只返回测试相关文件，传入 `".py"` 只返回 Python 文件。

- 适用场景：了解项目结构、定位特定类型的文件
- Agent 策略：当需要了解项目整体布局或找到某个文件的位置时调用

### `read_file`

```python
async def read_file(ctx: RunContext[RepoDeps], file_path: str) -> str
```

读取指定文件的**完整内容**。`file_path` 应为 `list_repo_files` 返回的相对路径。超过 8000 字符的文件会被截断，并显示总字符数。

- 适用场景：需要了解某个文件的完整上下文，而不仅仅是片段
- Agent 策略：在 `search_code` 找到相关文件后，有时会进一步调用此工具获取完整实现

---

## 7. 代码解析策略

### Python 文件：AST 解析

对所有 `.py` 文件使用 Python 标准库 `ast` 模块进行语法树解析：

- 提取所有顶层及一级嵌套的 `FunctionDef`、`AsyncFunctionDef`、`ClassDef`
- 每个定义块成为一个独立的 `CodeChunk`，包含精确的起止行号
- 若文件无任何函数/类（如纯配置文件），或存在 `SyntaxError`，则退回滑动窗口分块

### 非 Python 文件：滑动窗口

对其他所有支持的扩展名使用滑动窗口切分：

- 估算方式：1 token ≈ 4 个字符
- 每块上限：`MAX_CHUNK_TOKENS * 4 = 2048` 字符
- 块类型标记为 `"other"`，名称为文件名

### 支持的文件扩展名

```
.py  .js  .ts  .tsx  .jsx
.go  .java  .rs  .cpp  .c
.rb  .swift  .kt  .scala
.sh  .yaml  .yml  .toml  .json  .md
```

### 跳过的目录

```
.git  node_modules  __pycache__  .venv  venv  dist  build  .next  vendor
```

---

## 8. 项目结构

```
codebase-qa/
├── app.py                  # Streamlit 主入口，UI 逻辑
├── config.py               # 全局配置（模型、路径、扩展名白名单）
├── requirements.txt        # Python 依赖
├── .env                    # API Key（需自行创建，不提交到 git）
│
├── indexer/                # 仓库索引模块
│   ├── __init__.py         # 对外暴露 clone_repo/chunk_repo/index_chunks/is_indexed
│   ├── cloner.py           # GitPython 浅克隆，repos/ 目录管理
│   ├── chunker.py          # AST 解析 + 滑动窗口，CodeChunk dataclass
│   └── vector_store.py     # ChromaDB 读写，嵌入函数，语义检索
│
├── agent/                  # AI Agent 模块
│   ├── __init__.py         # 对外暴露 ask/ask_stream/RepoDeps
│   ├── qa_agent.py         # Pydantic AI Agent 定义，ask() 和 ask_stream()
│   └── tools.py            # 三个工具函数 + RepoDeps dataclass
│
├── chroma_db/              # ChromaDB 持久化目录（自动创建）
└── repos/                  # 克隆的仓库目录（自动创建）
```

---

## 9. 常见问题

**Q: 索引大型仓库（如 React、Django）时很慢怎么办？**

大型仓库可能产生数千个代码块。克隆使用 `depth=1` 已尽量减少下载量，但解析和嵌入步骤仍需时间。嵌入计算在 CPU 上进行，首次索引耗时与仓库大小成正比，一般 1000 个文件约需 1-3 分钟。索引一次后会持久化，无需重复操作。

**Q: 首次启动时卡在下载嵌入模型？**

`all-MiniLM-L6-v2` 模型（约 90MB）首次使用时由 `sentence-transformers` 自动从 Hugging Face 下载，缓存在本地 `~/.cache/huggingface/`。如网络受限，可手动下载后放置到缓存目录，或配置 HF 镜像：

```bash
export HF_ENDPOINT=https://hf-mirror.com
```

**Q: 如何对已索引的仓库重新索引（仓库有更新）？**

在侧边栏输入同一仓库 URL，若检测到已索引，会显示"已索引 ✓"和"重新索引"按钮。点击"重新索引"会清空旧集合并重新执行完整索引流程。注意：本地克隆不会自动 pull，如需拉取最新代码，可手动删除 `repos/owner__reponame/` 目录后重新索引。

**Q: 私有仓库能否索引？**

当前 `clone_repo()` 直接调用 `Repo.clone_from()`，依赖本地 git 凭证配置。只要本地 git 有权限 clone 该仓库（SSH key 或 HTTPS token），即可正常使用。

**Q: 向量数据库存在哪里？多个仓库会冲突吗？**

所有向量数据存储在 `chroma_db/` 目录，每个仓库对应一个独立的 ChromaDB collection，命名规则为 `{owner}-{reponame}`（全小写，最长 63 字符）。多个仓库完全隔离，互不影响。

**Q: 会话刷新后聊天记录丢失？**

Streamlit 的 `session_state` 仅在当前浏览器会话内有效，刷新页面后聊天记录会清空。向量索引是持久化的（存在 `chroma_db/`），重新打开页面后只需再次输入仓库 URL，无需重新索引。

**Q: read_file 有字符限制，读不到完整文件怎么办？**

`read_file` 工具当前截断阈值为 8000 字符。对于超长文件，可以在提问时指明需要看的具体部分，Agent 通常会先用 `search_code` 定位到相关代码片段，再按需调用 `read_file`，从而避免读取整个文件。
