"""Observe the SDK boundary without changing provider request settings."""

from dataclasses import asdict
import json

from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models.wrapper import WrapperModel


class RecordedModel(WrapperModel):
    def __init__(self, model):
        super().__init__(model)
        self.begin_turn()

    @property
    def name(self):
        return self.model_name

    def begin_turn(self):
        self.requests = []
        self.responses = []

    def model(self):
        return self

    async def request(self, messages, model_settings, model_request_parameters):
        record = {"messages": json.loads(ModelMessagesTypeAdapter.dump_json(messages)),
                  "model_settings": model_settings, "request_parameters": asdict(model_request_parameters), "status": "started"}
        self.requests.append(record)
        try:
            response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        except BaseException as exc:
            record.update(status="failed", error_type=type(exc).__name__)
            raise
        record["status"] = "completed"
        self.responses.append({"request_number": len(self.requests),
                               "message": json.loads(ModelMessagesTypeAdapter.dump_json([response]))[0]})
        return response
