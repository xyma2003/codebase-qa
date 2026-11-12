"""Opt-in indexed evaluations using the selected provider and successful baseline."""

import httpx
import json
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from evaluation.dataset import digest, validate_bundle
from evaluation.indexes import check_indexes
from evaluation.provider_model import RecordedModel
from evaluation.real_preflight import check_baseline, check_runtime
from evaluation.runner import _run


async def run_real(dataset, repository, output, *, baseline_directory, index_directory, fixture_model=None, turn_timeout=30, retrieval_strategy="semantic"):
    if retrieval_strategy not in {"semantic", "hybrid"}:
        raise ValueError("Unknown evaluation retrieval strategy")
    if type(turn_timeout) not in {int, float} or not 0 < turn_timeout <= 300:
        raise ValueError("Evaluation timeout must be between 0 and 300 seconds")
    fixture = fixture_model is not None
    if fixture and (not isinstance(fixture_model, OpenAIChatModel)
                    or not isinstance(fixture_model.provider.client._client._transport, httpx.MockTransport)):
        raise ValueError("Injected evaluation models must use an explicit in-memory MockTransport")
    baseline = check_baseline(baseline_directory)
    check_runtime(baseline, fixture=fixture)
    bundle = validate_bundle(dataset, repository)
    indexes = check_indexes(index_directory, bundle, repository)
    from config import OPENAI_API_KEY, OPENAI_API_BASE, OPENAI_MODEL
    if fixture:
        model = fixture_model
    else:
        model = OpenAIChatModel(OPENAI_MODEL, provider=OpenAIProvider(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE))
    if str(model.provider.base_url).rstrip("/") != "https://api.siliconflow.cn/v1" or model.model_name != OPENAI_MODEL:
        raise ValueError("Provider/model differs from the selected successful configuration")
    backend = RecordedModel(model)
    profile = {"format_version": 2, "backend": "provider_fixture" if fixture else "siliconflow",
               "real_model_called": not fixture, "model_name": OPENAI_MODEL, "model_settings": {},
               "provider_base_url": OPENAI_API_BASE, "sdk_max_retries": model.provider.client.max_retries,
               "index": {"status": "ready", "source_only": False, "manifest_sha256": digest(json.dumps(indexes, ensure_ascii=False, indent=2).encode() + b'\n')},
               "answer_cache": "disabled_for_fixed_evaluation", "baseline_fingerprint": baseline["fingerprint"],
               "usage_basis": "fixture_provider_response" if fixture else "provider_reported_tokens_cost_unknown",
               "retrieval_strategy": retrieval_strategy,
               "configuration_changes": {"source": "fixed_dataset_versions", "prompt_and_tools": "current_recorded_runtime",
                                         "retrieval_strategy": {"baseline": "semantic", "current": retrieval_strategy},
                                         "answer_cache": "disabled", "index_namespace": "dedicated_evaluation_collection",
                                         "conversation_store": "isolated_in_this_run"}}
    if fixture:
        secret = model.provider.client.api_key
    else:
        secret = OPENAI_API_KEY
    async with backend:
        return await _run(dataset, repository, output, backend=backend, turn_timeout=turn_timeout,
            indexed={"profile": profile, "baseline": baseline, "baseline_directory": baseline_directory, "indexes": indexes, "secret": secret})
