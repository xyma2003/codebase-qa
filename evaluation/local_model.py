"""A deterministic SDK fixture. It never consumes gold or answers benchmark tasks."""

import json
import re

from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel


class LocalModel:
    name = "local-evaluation-v1"

    def __init__(self, *, fail_question=None):
        self.fail_question = fail_question
        self.requests = []

    def begin_turn(self):
        self.requests = []

    def model(self):
        return FunctionModel(self.respond, model_name=self.name)

    async def respond(self, messages, info):
        self.requests.append({
            "messages": json.loads(ModelMessagesTypeAdapter.dump_json(messages)),
            "instructions": info.instructions,
            "model_settings": info.model_settings,
            "tools": [{"name": tool.name, "description": tool.description, "schema": tool.parameters_json_schema}
                      for tool in (*info.function_tools, *info.output_tools)],
        })
        prompts = [part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
        latest = max(index for index, message in enumerate(messages)
                     if any(isinstance(part, UserPromptPart) for part in message.parts))
        returned = [part for message in messages[latest:] for part in message.parts
                    if isinstance(part, ToolReturnPart) and part.tool_name in {"find_code", "read_file"}]
        # Inject a failure after a real locator result for failure/archive tests.
        if prompts[-1] == self.fail_question and returned:
            raise RuntimeError("LOCAL_FIXTURE_FAILURE_AFTER_TOOL")
        if not returned:
            return ModelResponse(parts=[ToolCallPart("find_code", {"query": "def ", "mode": "text", "limit": 1})])
        located = next(part for part in returned if part.tool_name == "find_code")
        hits = json.loads(located.content)["hits"]
        if not hits:
            raise RuntimeError("Local fixture requires at least one source definition")
        hit = hits[0]
        if returned[-1].tool_name == "find_code":
            return ModelResponse(parts=[ToolCallPart("read_file", {"file_path": hit["file_path"],
                "start_line": hit["start_line"], "end_line": hit["start_line"] + 2})])
        match = re.search(r"\[(S\d+)\]", str(returned[-1].content))
        if not match:
            raise RuntimeError("Local fixture did not receive a checked source identifier")
        reference = match[1]
        if info.output_tools:
            proposal = {
                "goal": "本地模拟：只验证规划运行和归档流程",
                "current_behavior": [{"description": "工具已读取当前快照的源码片段", "references": [reference]}],
                "file_changes": [{"file_path": hit["file_path"], "action": "modify", "reason": "本地流程样例",
                    "changes": ["演示修改清单，不执行文件修改"], "references": [reference]}],
                "steps": [{"description": "本地模拟步骤", "file_paths": [hit["file_path"]]}],
                "validation": [{"method": "检查本地归档是否完整", "expected_result": "输入输出与工具记录可回读", "command": None}],
                "open_questions": ["真实需求的方案质量尚未评测"],
            }
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, proposal)])
        return ModelResponse(parts=[TextPart(
            f"本地模拟：当前会话含 {len(prompts)} 个问题，已读取 {hit['file_path']} [{reference}]。只验证运行与归档，不代表题目已答对。"
        )])
