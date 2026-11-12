"""Create an explicitly local adversarial display sample; no provider or image requests."""

import argparse
import asyncio
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic_ai import models
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from agent import qa_agent
from agent.conversations import create_conversation
from agent.tools import RepoDeps
from indexer.repository_service import get_current_repository
from security.output import safe_answer_html


async def check():
    repository = get_current_repository("https://github.com/xyma2003/codebase-qa")
    if repository is None:
        raise ValueError("先选择已有索引，本检查不新建或更新索引。")
    session = create_conversation(repository.repo_url, repository.commit_sha, repository.collection_name)
    def respond(messages, info):
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": "config.py", "start_line": 1, "end_line": 12})])
        reference = re.search(r"\[(S\d+)\]", returns[-1].content)[1]
        return ModelResponse(parts=[TextPart(f"本地安全检查：已读取真实配置源码 [{reference}]。\n\n"
            "图片占位：![LOCAL_CANARY](https://example.invalid/pixel?value=LOCAL_ONLY)\n\n"
            "外部链接：[外部占位](https://example.invalid/)。本样例只检查展示，不代表真实模型防御效果。")])
    deps = RepoDeps(repository.repo_url, repository.repo_root, repository.collection_name, repository.commit_sha,
                    source_only=True, use_answer_cache=False)
    original = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = False
    try:
        checked = await qa_agent._run_answer("本地安全检查：验证回答展示", deps, session,
                                             agent=qa_agent.build_agent(FunctionModel(respond)))
    finally:
        models.ALLOW_MODEL_REQUESTS = original
    rendered = safe_answer_html(checked.text, source_urls=[source.url for source in checked.citations])
    assert "<img" not in rendered and 'href="https://example.invalid' not in rendered
    assert checked.can_save
    return {"status": "local_security_display_fixture_passed", "conversation_id": session,
            "source_commit": repository.commit_sha, "active_index_unchanged": repository == get_current_repository(repository.repo_url),
            "real_model_requests": 0, "external_image_requests_by_script": 0,
            "semantic_attack_resistance_evaluated": False, "html": rendered}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("Choose a new output path")
    result = asyncio.run(check())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({key: result[key] for key in ("status", "conversation_id", "real_model_requests")}))
