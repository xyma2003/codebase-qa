"""
memory/quality.py — 问答质量评估

纯 Python 规则，不调 LLM，零延迟。
评分 0.0-1.0，>= QUALITY_THRESHOLD 才保存到知识库。
"""
import re


# 技术词正则：函数名/类名格式（含下划线、驼峰）或常见技术关键词
_TECH_PATTERN = re.compile(
    r'\b([A-Z][a-z]+[A-Z]\w*|[a-z]+_[a-z_]+|def\s+\w+|class\s+\w+|'
    r'import\s+\w+|async|await|lambda|yield|decorator|middleware|'
    r'endpoint|handler|callback|hook|context|state|graph|node|edge|'
    r'token|embed|vector|chunk|index|schema|model|agent|tool)\b'
)

_UNCERTAINTY_PATTERN = re.compile(
    r'你确定吗|能详细说说吗|我不太明白|什么意思|能再解释|'
    r'are you sure|can you elaborate|i don\'t understand|'
    r'what do you mean|explain more',
    re.IGNORECASE
)

_FILE_PATH_PATTERN = re.compile(r'\b[\w/\-\.]+\.(py|js|ts|go|java|rs|cpp|rb)\b')


def evaluate(question: str, answer: str, conversation: list[dict] | None = None) -> float:
    """
    评估一条问答对的质量，返回 0.0-1.0 的分数。

    Args:
        question: 用户的问题
        answer:   Agent 的回答
        conversation: 完整对话历史（list of {"role": ..., "content": ...}），
                      用于检测用户是否追问/不满意。可为 None。

    Returns:
        float: 质量分数，>= 0.6 建议保存
    """
    score = 0.0

    # +0.3 答案里有文件路径（有具体代码证据）
    if _FILE_PATH_PATTERN.search(answer):
        score += 0.3

    # +0.3 答案足够详细（> 200 字）
    if len(answer.strip()) > 200:
        score += 0.3

    # +0.2 问题包含具体技术词
    if _TECH_PATTERN.search(question):
        score += 0.2

    # +0.2 对话中用户没有追问不满意的话
    if conversation:
        user_msgs = [m["content"] for m in conversation if m.get("role") == "user"]
        # 跳过第一条（原始问题），看后续有没有不满意的追问
        followups = user_msgs[1:]
        has_dissatisfaction = any(_UNCERTAINTY_PATTERN.search(m) for m in followups)
        if not has_dissatisfaction:
            score += 0.2
    else:
        # 没有对话历史时，默认给满分这一项
        score += 0.2

    return round(score, 2)
