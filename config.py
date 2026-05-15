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

# Self-improving memory 配置
MEMORY_SIMILARITY_THRESHOLD = 0.45   # memory 命中阈值（ChromaDB L2 距离转相似度）
QUALITY_THRESHOLD = 0.6              # 问答质量评分阈值，>= 此值才保存
