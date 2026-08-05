# Research: Codebase Q&A Agent with Pydantic AI

**Researched:** 2026-05-07
**Pydantic AI version verified:** 1.91.0 (released May 7, 2026)
**Confidence:** HIGH (official docs + GitHub source verified)

---

## 1. Pydantic AI Current API (v1.91.0)

### Core Classes

**`Agent[DepsType, OutputType]`** — the central class. Generic in two type parameters: dependency type and output type.

```python
from pydantic_ai import Agent, RunContext
from dataclasses import dataclass

@dataclass
class Deps:
    vector_db: ChromaCollection
    repo_path: str

agent = Agent(
    'anthropic:claude-opus-4-6',
    deps_type=Deps,
    output_type=str,           # or a Pydantic model for structured output
    instructions='You are a codebase expert. Answer questions about the repo.',
)
```

**`RunContext[DepsType]`** — injected as the first parameter in every tool. Provides `.deps` for accessing the typed dependency object.

**`@agent.tool`** vs **`@agent.tool_plain`**:
- `@agent.tool` — receives `RunContext` as first param; use this for 99% of tools
- `@agent.tool_plain` — no context; for pure utility functions

### Running the Agent

Three execution modes:
```python
# Synchronous (blocks, fine for CLI)
result = agent.run_sync('question', deps=my_deps)
print(result.output)

# Async
result = await agent.run('question', deps=my_deps)
print(result.output)

# Streaming async
async with agent.run_stream('question', deps=my_deps) as result:
    async for chunk in result.stream_output(debounce_by=None):
        print(chunk, end='', flush=True)
```

**CRITICAL RENAME (v0.1.0 / v0.6.0):** `result.data` was removed. Use `result.output`. `result_type` param is now `output_type`. Any tutorial older than April 2025 using `.data` is broken.

### Tool Definition Pattern

```python
@agent.tool
async def search_code(ctx: RunContext[Deps], query: str) -> str:
    """Search codebase for relevant code snippets.

    Args:
        query: A natural language description of what code to find.
    """
    results = ctx.deps.vector_db.query(query_texts=[query], n_results=8)
    return format_results(results)
```

- Docstring = tool description sent to the LLM
- Google/NumPy/Sphinx docstring formats supported for parameter descriptions
- Return any JSON-serializable type; strings are most reliable

### Dynamic Instructions

```python
@agent.instructions
async def system_prompt(ctx: RunContext[Deps]) -> str:
    return f"The codebase is at {ctx.deps.repo_path}. Languages present: Python, JavaScript."
```

---

## 2. Anthropic Claude Integration

### Installation

```bash
pip install "pydantic-ai-slim[anthropic]"
# or the full bundle:
pip install pydantic-ai
```

### Configuration

```python
import os
os.environ['ANTHROPIC_API_KEY'] = 'sk-ant-...'

# Short form (recommended)
agent = Agent('anthropic:claude-opus-4-6')

# Explicit form with settings
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings

model = AnthropicModel(
    'claude-opus-4-6',
    settings=AnthropicModelSettings(
        temperature=0.2,
        max_tokens=4096,
    )
)
agent = Agent(model)
```

### Prompt Caching (cost-saving for large system prompts)

```python
settings = AnthropicModelSettings(anthropic_cache=True)
```

This is valuable for codebase agents where the system prompt may include large context windows of retrieved code.

---

## 3. Vector Database Recommendation: ChromaDB

### Verdict: ChromaDB over Qdrant over FAISS for this use case

| DB | Local/Embedded | Server Required | Python API | Persistence |
|----|---------------|-----------------|-----------|-------------|
| ChromaDB | YES | NO | Simple | `PersistentClient(path=...)` |
| Qdrant | YES | NO (client mode) | Good | `QdrantClient(path=...)` |
| FAISS | YES | NO | Low-level | Manual pickle/npy files |

**Use ChromaDB because:**
1. Zero infrastructure — `PersistentClient(path="./chroma_db")` is all you need
2. Built-in embedding function support (SentenceTransformers, OpenAI, etc.)
3. `get_or_create_collection()` is idempotent — safe to re-run indexing
4. Apache 2.0 license, actively maintained
5. Handles metadata filtering natively (filter by file, language, etc.)

**Qdrant is the runner-up** if you need richer filtering or plan to scale to a server later — same local API, just swap `QdrantClient(path=...)` to `QdrantClient("localhost", 6333)`.

**Avoid FAISS** for this project: no metadata storage, no persistence abstraction, requires manual serialization — adds complexity with no benefit at this scale.

### ChromaDB Setup

```python
import chromadb
from chromadb.utils import embedding_functions

# Persistent local client — data survives restarts
client = chromadb.PersistentClient(path="./chroma_db")

# Use sentence-transformers for local, free embeddings
ef = embedding_functions.SentenceTransformerEmbeddingFunction(
    model_name="all-MiniLM-L6-v2"
)

collection = client.get_or_create_collection(
    name="codebase",
    embedding_function=ef,
    metadata={"hnsw:space": "cosine"}
)
```

---

## 4. Embedding Model Recommendation

### For code + natural language queries: `all-MiniLM-L6-v2`

- 22M params, very fast CPU inference (~14ms per sentence)
- Good general-purpose semantic similarity
- Works well for mixed code+comment+docstring chunks
- 384-dimensional vectors — efficient storage

### For code-specific search: `microsoft/graphcodebert-base`

- Pre-trained on CodeSearchNet (6 languages: Python, JS, Ruby, Go, Java, PHP)
- Understands data-flow semantics, not just token sequences
- 768-dimensional, ~slower than MiniLM
- Better at "find the function that does X" queries

**Recommendation:** Start with `all-MiniLM-L6-v2` for simplicity and speed. Swap to `graphcodebert-base` if retrieval quality is poor on code-specific queries.

```bash
pip install sentence-transformers
```

---

## 5. Code Chunking Strategy

### Recommended: AST-based chunking (function/class level)

Do NOT use naive line-count sliding windows for code. Code structure matters.

**Strategy:**

1. **Python files** — use `ast` module to extract top-level functions and classes as independent chunks
2. **JS/TS files** — use `tree-sitter` or regex-based extraction for function/class blocks
3. **Other files** — fall back to line-based sliding window (512 tokens, 64-token overlap)

**Chunk metadata to store (critical for useful answers):**

```python
{
    "file_path": "src/auth/login.py",
    "language": "python",
    "chunk_type": "function",   # function | class | module | block
    "name": "validate_token",
    "start_line": 42,
    "end_line": 67,
    "repo_url": "https://github.com/org/repo",
}
```

**Max chunk size:** ~1500 tokens (leave room in context window for the answer)
**Overlap:** Only needed for sliding window fallback; not needed for AST chunks since they are semantically complete.

### Python AST Chunking Example

```python
import ast

def extract_python_chunks(source: str, file_path: str) -> list[dict]:
    tree = ast.parse(source)
    chunks = []
    lines = source.splitlines()

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Only top-level and class-level (not nested functions)
            start = node.lineno - 1
            end = node.end_lineno
            chunk_text = "\n".join(lines[start:end])
            chunks.append({
                "text": chunk_text,
                "metadata": {
                    "file_path": file_path,
                    "chunk_type": type(node).__name__,
                    "name": node.name,
                    "start_line": node.lineno,
                    "end_line": node.end_lineno,
                }
            })
    return chunks
```

---

## 6. Streaming in Pydantic AI

```python
import asyncio

async def stream_answer(question: str, deps: Deps) -> None:
    async with agent.run_stream(question, deps=deps) as result:
        async for chunk in result.stream_output(debounce_by=None):
            print(chunk, end='', flush=True)
    print()  # newline after streaming completes

# For CLI use
asyncio.run(stream_answer("Where is the auth middleware?", deps))
```

**Known issue (v1.88+):** Cancellation cleanup in `run_stream_events` may return prematurely (GitHub #5132). Workaround: use `run_stream` (not `run_stream_events`) for simple text streaming.

**Partial output flag:** If using output validators with side effects, check `RunContext.partial_output` to avoid executing side effects on intermediate chunks.

---

## 7. Project Structure

```
codebase-qa/
├── agent.py              # Agent definition, tool decorators, deps dataclass
├── indexer.py            # Clone repo, parse files, chunk, store embeddings
├── chunker.py            # AST/tree-sitter chunking logic per language
├── retriever.py          # ChromaDB wrapper (query, add, reset)
├── ui.py                 # Streamlit UI or CLI entry point
├── config.py             # Settings (model name, chunk size, db path, etc.)
├── requirements.txt
└── .env                  # ANTHROPIC_API_KEY
```

### Module Responsibilities

| Module | Responsibility |
|--------|---------------|
| `agent.py` | Agent + tool definitions; the brain |
| `indexer.py` | Orchestrates cloning, walking files, chunking, calling retriever.add() |
| `chunker.py` | Language-specific chunking: Python AST, JS regex, fallback sliding window |
| `retriever.py` | ChromaDB client wrapper; query() and add() with metadata |
| `ui.py` | User-facing interface (Streamlit or Click CLI) |
| `config.py` | Pydantic Settings model for config/env management |

---

## 8. UI Recommendation: Streamlit

**Use Streamlit over CLI** because:
- Shows streaming output naturally with `st.write_stream`
- Lets users paste GitHub URLs in a text input
- Displays retrieved code chunks in expandable blocks with syntax highlighting
- Session state can cache the indexed repo so re-indexing is not triggered per question

**Use CLI** only if you want zero extra dependencies or want to pipe output.

For the codebase agent, Streamlit is the better fit:

```bash
pip install streamlit
streamlit run ui.py
```

---

## 9. Common Pitfalls

### CRITICAL

**Pitfall 1: Using old `result.data` API**
The `result.data` attribute was removed in v0.6.0 (August 2025). Use `result.output`. Any tutorial/blog post before mid-2025 likely uses the old API. The full rename map:
- `result.data` → `result.output`
- `Agent(result_type=...)` → `Agent(output_type=...)`
- `StreamedRunResult.get_data()` → `StreamedRunResult.get_output()`

**Pitfall 2: `run_sync` inside Jupyter / async context**
`run_sync` internally calls `asyncio.run()`. If you are already inside an async context (Jupyter, Streamlit's async server), this raises `RuntimeError: This event loop is already running`.

Fixes:
```python
# Option A: use await directly in async context
result = await agent.run('question', deps=deps)

# Option B: install nest_asyncio for Jupyter
import nest_asyncio
nest_asyncio.apply()
result = agent.run_sync('question', deps=deps)
```

**Pitfall 3: `prepare_tools` returning None strips all tools**
If you use the `prepare_tools` capability callback and return `None` (instead of an empty list or the original tools), ALL tools are silently dropped. This is a known bug (GitHub #5177). Always explicitly return the tools list.

### MODERATE

**Pitfall 4: ChromaDB collection resets on `create_collection` (not `get_or_create_collection`)**
Always use `client.get_or_create_collection()` for idempotent indexing. `create_collection()` raises an error if the collection already exists.

**Pitfall 5: Embedding dimension mismatch after changing models**
If you change the embedding model (e.g., MiniLM to GraphCodeBERT), you MUST delete and recreate the ChromaDB collection — the dimensions are different (384 vs 768). ChromaDB will silently fail or error on mismatch.

**Pitfall 6: Anthropic tool-call loops**
Anthropic Claude will sometimes call the same retrieval tool repeatedly with slightly different queries. Set `max_steps` (or the legacy `max_retries`) on the Agent or tool to prevent infinite loops:
```python
agent = Agent('anthropic:claude-opus-4-6', ..., max_steps=10)
```

**Pitfall 7: Cloning large repos is slow**
Use shallow clone for faster initial indexing:
```python
import subprocess
subprocess.run(['git', 'clone', '--depth=1', repo_url, target_dir], check=True)
```

### MINOR

**Pitfall 8: Python 3.9 dropped in v1.0.0 (September 2025)**
Requires Python 3.10+. Use `python_requires=">=3.10"` in your project.

**Pitfall 9: Agent hangs with local Ollama endpoints**
If you experiment with local models via Ollama, there is a known hang issue (GitHub #4681). Anthropic and OpenAI are unaffected.

**Pitfall 10: Capabilities API changed in v1.88.0**
`prepare_tools` now only applies to function tools (not output tools). The new `prepare_output_tools` hook was added separately. If upgrading from pre-v1.88, audit any `prepare_tools` usage.

---

## 10. Skeleton: `agent.py`

```python
"""
Codebase Q&A Agent using Pydantic AI + ChromaDB.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import chromadb
from chromadb.utils import embedding_functions

from pydantic_ai import Agent, RunContext

# ---------------------------------------------------------------------------
# Dependencies (injected into every tool via RunContext)
# ---------------------------------------------------------------------------

@dataclass
class Deps:
    collection: chromadb.Collection
    repo_path: str
    repo_url: str


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

agent = Agent(
    'anthropic:claude-opus-4-6',
    deps_type=Deps,
    output_type=str,
    instructions=(
        "You are a codebase expert. Answer questions about the repository "
        "by searching for relevant code. Always cite the file path and line "
        "numbers when referencing specific code. If you can't find something "
        "after searching, say so honestly."
    ),
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@agent.tool
async def search_code(ctx: RunContext[Deps], query: str) -> str:
    """Search the codebase for code relevant to the query.

    Args:
        query: Natural language description of the code or concept to find.
               Be specific — e.g. 'function that validates JWT tokens' rather
               than just 'auth'.
    """
    results = ctx.deps.collection.query(
        query_texts=[query],
        n_results=8,
        include=["documents", "metadatas", "distances"],
    )

    if not results["documents"] or not results["documents"][0]:
        return "No relevant code found for this query."

    chunks = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        relevance = 1 - dist  # cosine: lower distance = higher relevance
        chunks.append(
            f"File: {meta.get('file_path', 'unknown')} "
            f"(lines {meta.get('start_line', '?')}-{meta.get('end_line', '?')}) "
            f"[relevance: {relevance:.2f}]\n"
            f"```{meta.get('language', '')}\n{doc}\n```"
        )

    return "\n\n---\n\n".join(chunks)


@agent.tool
async def list_files(ctx: RunContext[Deps], pattern: str = "") -> str:
    """List files in the repository, optionally filtered by a pattern.

    Args:
        pattern: Optional glob or keyword to filter files, e.g. 'auth', '.py'.
    """
    import os

    all_files = []
    for root, dirs, files in os.walk(ctx.deps.repo_path):
        # Skip hidden dirs and common noise dirs
        dirs[:] = [
            d for d in dirs
            if not d.startswith('.')
            and d not in {'node_modules', '__pycache__', '.git', 'dist', 'build'}
        ]
        for f in files:
            rel_path = os.path.relpath(os.path.join(root, f), ctx.deps.repo_path)
            if not pattern or pattern.lower() in rel_path.lower():
                all_files.append(rel_path)

    if not all_files:
        return f"No files found matching '{pattern}'."

    return f"Found {len(all_files)} files:\n" + "\n".join(sorted(all_files)[:100])


@agent.tool
async def read_file(ctx: RunContext[Deps], file_path: str) -> str:
    """Read the full content of a specific file in the repository.

    Args:
        file_path: Relative path from the repo root, e.g. 'src/auth/login.py'.
    """
    import os

    full_path = os.path.join(ctx.deps.repo_path, file_path)
    full_path = os.path.normpath(full_path)

    # Security: prevent path traversal
    if not full_path.startswith(os.path.normpath(ctx.deps.repo_path)):
        return "Error: path traversal attempt detected."

    if not os.path.isfile(full_path):
        return f"File not found: {file_path}"

    try:
        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        lines = content.splitlines()
        if len(lines) > 300:
            return (
                f"File has {len(lines)} lines. Showing first 300:\n\n"
                + "\n".join(f"{i+1:4}: {l}" for i, l in enumerate(lines[:300]))
            )
        return "\n".join(f"{i+1:4}: {l}" for i, l in enumerate(lines))
    except Exception as e:
        return f"Error reading file: {e}"


# ---------------------------------------------------------------------------
# Convenience runner
# ---------------------------------------------------------------------------

async def ask(question: str, deps: Deps) -> str:
    """Run the agent and return the answer as a string."""
    result = await agent.run(question, deps=deps)
    return result.output


async def ask_stream(question: str, deps: Deps) -> None:
    """Stream the agent's answer to stdout."""
    async with agent.run_stream(question, deps=deps) as result:
        async for chunk in result.stream_output(debounce_by=None):
            print(chunk, end='', flush=True)
    print()


# ---------------------------------------------------------------------------
# Deps factory
# ---------------------------------------------------------------------------

def build_deps(repo_path: str, repo_url: str, db_path: str = "./chroma_db") -> Deps:
    """Create the dependency object for the agent."""
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    client = chromadb.PersistentClient(path=db_path)
    collection = client.get_or_create_collection(
        name="codebase",
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )
    return Deps(collection=collection, repo_path=repo_path, repo_url=repo_url)
```

---

## 11. Full Architecture Diagram

```
User Input (GitHub URL + question)
          |
          v
    [indexer.py]
    git clone --depth=1 <url>
          |
          v
    [chunker.py]
    Walk all files
    Python  -> ast.parse() -> function/class chunks
    JS/TS   -> tree-sitter or regex -> function chunks
    Others  -> sliding window (512 tok, 64 overlap)
          |
          v
    [retriever.py]
    ChromaDB PersistentClient
    get_or_create_collection("codebase")
    collection.upsert(documents, metadatas, ids)
          |
          v
    [agent.py] Deps{collection, repo_path, repo_url}
    Agent('anthropic:claude-opus-4-6')
    |
    +--> @agent.tool search_code(query) -> ChromaDB.query()
    +--> @agent.tool list_files(pattern) -> os.walk()
    +--> @agent.tool read_file(path) -> open()
          |
          v
    [ui.py]
    Streamlit: st.text_input (URL), st.chat_input (question)
               st.write_stream (answer)
    or CLI:    asyncio.run(ask_stream(question, deps))
```

---

## 12. Key Dependencies

```txt
# requirements.txt
pydantic-ai[anthropic]>=1.91.0
chromadb>=0.6.0
sentence-transformers>=3.0.0
streamlit>=1.40.0          # if using Streamlit UI
gitpython>=3.1.0           # for repo cloning
python-dotenv>=1.0.0
```

Optional for better JS/TS chunking:
```txt
tree-sitter>=0.23.0
tree-sitter-python>=0.23.0
tree-sitter-javascript>=0.23.0
tree-sitter-typescript>=0.23.0
```

---

## 13. Answers to Key Questions (Summary)

| Question | Answer |
|----------|--------|
| Latest Pydantic AI version | 1.91.0 (May 7, 2026), Python 3.10+ required |
| Tool definition | `@agent.tool` decorator; first param is `RunContext[Deps]`; docstring = tool description |
| `RunContext` | `.deps` attribute gives typed dependency; available in tools and `@agent.instructions` |
| Best vector DB | ChromaDB with `PersistentClient(path=...)` — no server, embedded, simple API |
| Anthropic integration | `Agent('anthropic:claude-opus-4-6')` + `ANTHROPIC_API_KEY` env var; `pydantic-ai[anthropic]` package |
| Streaming | `async with agent.run_stream(...) as r: async for chunk in r.stream_output():` |
| Code chunking | AST-based (function/class level) for Python; tree-sitter for JS; sliding window fallback |
| Project structure | agent.py / indexer.py / chunker.py / retriever.py / ui.py |
| UI | Streamlit for demos; CLI via `asyncio.run()` for scripts |
| Main pitfall | `result.data` removed — use `result.output`; `run_sync` breaks in async contexts |

---

## Sources

- PyPI pydantic-ai: https://pypi.org/project/pydantic-ai/
- Pydantic AI official docs: https://pydantic.dev/docs/ai/overview/
- Pydantic AI Anthropic docs: https://pydantic.dev/docs/ai/models/anthropic/
- Pydantic AI tools docs (GitHub raw): https://raw.githubusercontent.com/pydantic/pydantic-ai/main/docs/tools.md
- Pydantic AI dependency injection docs: https://raw.githubusercontent.com/pydantic/pydantic-ai/main/docs/dependencies.md
- Pydantic AI bank_support example: https://raw.githubusercontent.com/pydantic/pydantic-ai/main/examples/pydantic_ai_examples/bank_support.py
- Pydantic AI RAG example: https://github.com/pydantic/pydantic-ai/blob/main/examples/pydantic_ai_examples/rag.py
- Pydantic AI releases / changelog: https://github.com/pydantic/pydantic-ai/releases
- Pydantic AI open bugs: https://github.com/pydantic/pydantic-ai/issues?q=is%3Aissue+is%3Aopen+label%3Abug
- ChromaDB clients docs: https://docs.trychroma.com/docs/run-chroma/clients.md
- Qdrant local mode docs: https://github.com/qdrant/qdrant-client/blob/master/README.md
- GraphCodeBERT: https://huggingface.co/microsoft/graphcodebert-base
- Pydantic AI troubleshooting: https://raw.githubusercontent.com/pydantic/pydantic-ai/main/docs/troubleshooting.md
