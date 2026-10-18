"""Luna structured outputs use Responses and the configured SDK retry budget."""

import json

import httpx2
from pydantic import BaseModel
from pydantic_ai import Agent

from app.features.agents import llm


async def test_responses_luna_omits_sampling_and_preserves_tool_contract(monkeypatch):
    requests = []

    class Result(BaseModel):
        value: str

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert body["model"] == "gpt-6-luna"
        assert body["reasoning"]["effort"] == "low"
        assert "temperature" not in body
        assert body["max_output_tokens"] == 256
        tool = body["tools"][0]
        return httpx2.Response(
            200,
            json={
                "id": "resp_synthetic",
                "object": "response",
                "created_at": 0,
                "model": "gpt-6-luna",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_synthetic",
                        "call_id": "call_synthetic",
                        "name": tool["name"],
                        "arguments": '{"value":"synthetic result"}',
                        "status": "completed",
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            },
        )

    real = llm.OpenAIProvider

    def provider(**kwargs):
        if "openai_client" not in kwargs:
            kwargs["http_client"] = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        return real(**kwargs)

    monkeypatch.setattr(llm, "OpenAIProvider", provider)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    model = llm.resolve("openai-responses:gpt-6-luna", retries=0)
    assert model.client.max_retries == 0
    result = await Agent(model, output_type=Result).run(
        "Return the synthetic value",
        model_settings={"temperature": 0, "max_tokens": 256, "openai_reasoning_effort": "low"},
    )
    assert result.output.value == "synthetic result"
    assert len(requests) == 1
