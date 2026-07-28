import os
from dotenv import load_dotenv

load_dotenv(override=True)  # .env wins over stale shell vars

# LLM provider: "openai" (SiliconFlow/any OpenAI-compatible) or "anthropic"
# Defaults to "openai" if OPENAI_API_KEY is set, else "anthropic".
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.siliconflow.cn/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "Qwen/Qwen3-32B")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-5"

# Build Pydantic AI model string — prefer OpenAI-compatible (SiliconFlow),
# fall back to Anthropic.
if OPENAI_API_KEY:
    LLM_PROVIDER = "openai"
    LLM_MODEL_STRING = f"openai:{OPENAI_MODEL}"
    # Pydantic AI reads these env vars for the openai provider
    os.environ.setdefault("OPENAI_API_KEY", OPENAI_API_KEY)
    os.environ.setdefault("OPENAI_API_BASE", OPENAI_API_BASE)
else:
    LLM_PROVIDER = "anthropic"
    LLM_MODEL_STRING = f"anthropic:{CLAUDE_MODEL}"
    if ANTHROPIC_API_KEY:
        os.environ.setdefault("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY)

CHROMA_DIR = "chroma_db"
CLONE_DIR = "repos"
EMBED_MODEL = "all-MiniLM-L6-v2"

# 代码文件扩展名白名单
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx",
    ".go", ".java", ".rs", ".cpp", ".c",
    ".rb", ".swift", ".kt", ".scala",
    ".sh", ".yaml", ".yml", ".toml", ".json",
    ".md",
}

# 单 chunk 最大 token 数（滑动窗口 fallback 用）
MAX_CHUNK_TOKENS = 512

# Self-improving memory 配置
MEMORY_SIMILARITY_THRESHOLD = 0.45   # memory 命中阈值（ChromaDB L2 距离转相似度）
QUALITY_THRESHOLD = 0.6              # 问答质量评分阈值，>= 此值才保存
