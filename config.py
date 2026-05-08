import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Pydantic AI 使用原生 anthropic model string
CLAUDE_MODEL = "claude-sonnet-4-5"

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
