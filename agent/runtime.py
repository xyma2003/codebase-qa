"""Explicit per-turn limits; omitted settings preserve the SDK defaults."""

import asyncio
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.usage import UsageLimits

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class RunPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", allow_inf_nan=False)
    request_limit: PositiveInt | None = 50
    tool_calls_limit: NonNegativeInt | None = None
    total_tokens_limit: PositiveInt | None = None
    timeout_seconds: Annotated[float, Field(gt=0)] | None = None
    input_characters_limit: PositiveInt | None = None

    def check_input(self, question, history):
        # This is a character guard, not a model-specific context/token estimator.
        size = len(question) + (len(ModelMessagesTypeAdapter.dump_json(history).decode()) if history else 0)
        if self.input_characters_limit is not None and size > self.input_characters_limit:
            raise ValueError(f"问题和历史共 {size} 字符，超过设置的 {self.input_characters_limit} 字符上限。请新建对话或调整限制。")

    def usage_limits(self):
        return UsageLimits(request_limit=self.request_limit, tool_calls_limit=self.tool_calls_limit,
                           total_tokens_limit=self.total_tokens_limit)


class RunRecord(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    policy: RunPolicy | None = None
    retrieval_strategy: Literal["semantic", "hybrid"] = "semantic"
    use_answer_cache: bool = True
    elapsed_ms: NonNegativeInt
    requests: NonNegativeInt
    tool_calls: NonNegativeInt
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt


async def run_with_timeout(awaitable, seconds):
    if seconds is None:
        return await awaitable
    timer = asyncio.timeout(seconds)
    try:
        async with timer:
            return await awaitable
    except TimeoutError:
        if timer.expired():
            raise TimeoutError(f"本轮运行超过 {seconds:g} 秒，已停止等待。") from None
        raise
