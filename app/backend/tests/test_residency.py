"""Model provider routing (Anthropic / Bedrock / Vertex) and the India residency guard."""

from __future__ import annotations

import pytest

from cfo_agent.residency import bedrock_routing, processing_location, residency_errors
from cfo_agent.settings import Settings, get_settings


def S(**env) -> Settings:
    base = {"ANTHROPIC_API_KEY": "sk-ant-test-key", "LLM_PROVIDER": "anthropic"}
    base.update(env)
    return Settings(**base)


def test_bedrock_routing_prefixes():
    assert bedrock_routing("anthropic.claude-3-haiku-20240307-v1:0") == "in-region"
    assert bedrock_routing("in.anthropic.claude-sonnet-5") == "geo:in"
    assert bedrock_routing("global.anthropic.claude-sonnet-5-5") == "geo:global"
    assert bedrock_routing("apac.anthropic.claude-sonnet-5") == "geo:apac"
    assert bedrock_routing("arn:aws:bedrock:ap-south-1:123:application-inference-profile/x") == "unknown"


@pytest.mark.parametrize(
    "env,ok",
    [
        ({"LLM_PROVIDER": "anthropic"}, False),
        ({"LLM_PROVIDER": "bedrock", "AWS_REGION": "ap-south-1", "ANTHROPIC_MODEL": "in.anthropic.claude-sonnet-5"}, True),
        ({"LLM_PROVIDER": "bedrock", "AWS_REGION": "ap-south-2", "ANTHROPIC_MODEL": "anthropic.claude-opus-5"}, True),
        ({"LLM_PROVIDER": "bedrock", "AWS_REGION": "ap-south-1", "ANTHROPIC_MODEL": "global.anthropic.claude-sonnet-5-5"}, False),
        ({"LLM_PROVIDER": "bedrock", "AWS_REGION": "ap-south-1", "ANTHROPIC_MODEL": "apac.anthropic.claude-sonnet-5"}, False),
        ({"LLM_PROVIDER": "bedrock", "AWS_REGION": "us-east-1", "ANTHROPIC_MODEL": "anthropic.claude-opus-5"}, False),
        ({"LLM_PROVIDER": "vertex", "VERTEX_PROJECT_ID": "p", "VERTEX_REGION": "asia-south1"}, True),
        ({"LLM_PROVIDER": "vertex", "VERTEX_PROJECT_ID": "p", "VERTEX_REGION": "us-east5"}, False),
        ({"LLM_PROVIDER": "vertex", "VERTEX_PROJECT_ID": "p", "VERTEX_REGION": "global"}, False),
    ],
)
def test_india_residency_guard(env, ok):
    errs = residency_errors(S(DATA_RESIDENCY="india", **env))
    assert (errs == []) is ok, errs


def test_provider_config_required():
    assert "AWS_REGION" in " ".join(residency_errors(S(LLM_PROVIDER="bedrock")))
    assert "VERTEX_PROJECT_ID" in " ".join(residency_errors(S(LLM_PROVIDER="vertex")))
    assert residency_errors(S(LLM_PROVIDER="openai"))


def test_no_anthropic_key_needed_on_bedrock(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = Settings(_env_file=None, LLM_PROVIDER="bedrock", AWS_REGION="ap-south-1", NEWS_PROVIDER="off")
    assert s.anthropic_api_key is None
    with pytest.raises(ValueError):
        Settings(_env_file=None, LLM_PROVIDER="anthropic")
    with pytest.raises(ValueError):  # news via Anthropic still needs the key
        Settings(_env_file=None, LLM_PROVIDER="bedrock", AWS_REGION="ap-south-1", NEWS_PROVIDER="anthropic")


def test_clients_point_at_the_regional_endpoint(monkeypatch):
    from anthropic import AnthropicBedrock, AnthropicVertex

    from cfo_agent.llm import make_client

    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    get_settings.cache_clear()
    c = make_client()
    assert isinstance(c, AnthropicBedrock)
    assert "bedrock-runtime.ap-south-1.amazonaws.com" in str(c.base_url)

    monkeypatch.setenv("LLM_PROVIDER", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT_ID", "client-proj")
    monkeypatch.setenv("VERTEX_REGION", "asia-south1")
    get_settings.cache_clear()
    c = make_client()
    assert isinstance(c, AnthropicVertex)
    assert "asia-south1-aiplatform.googleapis.com" in str(c.base_url)
    assert c.project_id == "client-proj"
    get_settings.cache_clear()


def test_news_disabled_on_bedrock_unless_opted_in(monkeypatch):
    from cfo_agent.news import news_disabled_reason

    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    get_settings.cache_clear()
    assert "NEWS_PROVIDER=anthropic" in news_disabled_reason()
    monkeypatch.setenv("NEWS_PROVIDER", "anthropic")
    get_settings.cache_clear()
    assert news_disabled_reason() is None
    get_settings.cache_clear()


def test_processing_location_reported():
    loc = processing_location(S(LLM_PROVIDER="bedrock", AWS_REGION="ap-south-1",
                                ANTHROPIC_MODEL="in.anthropic.claude-sonnet-5", DATA_RESIDENCY="india"))
    assert "India" in loc["processed_in"] and loc["residency_policy"] == "india"
    assert "outside India" in processing_location(S())["processed_in"]


def test_startup_refuses_non_compliant_residency(monkeypatch):
    from cfo_agent.security import validate_deployment_settings

    monkeypatch.setenv("DATA_RESIDENCY", "india")
    get_settings.cache_clear()
    assert any("Anthropic API does not process in India" in e for e in validate_deployment_settings())
    get_settings.cache_clear()


def test_tool_mode_structured_output(monkeypatch):
    """On Bedrock/Vertex, parse() asks for a forced tool call and validates its input."""
    from types import SimpleNamespace

    from pydantic import BaseModel

    from cfo_agent import llm

    class Out(BaseModel):
        title: str
        score: float

    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=3, output_tokens=2),
                content=[SimpleNamespace(type="tool_use", name="record_answer", input={"title": "ok", "score": 1.5})],
            )

        def parse(self, **kw):  # must not be used in tool mode
            raise AssertionError("native parse used on bedrock")

    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    get_settings.cache_clear()
    monkeypatch.setattr(llm, "_client", lambda *a: SimpleNamespace(messages=FakeMessages()))
    out = llm.parse(Out, system="s", user="u", purpose="t", thinking=True)
    assert out == Out(title="ok", score=1.5)
    assert captured["tool_choice"] == {"type": "tool", "name": "record_answer"}
    assert captured["tools"][0]["input_schema"]["properties"]["score"]["type"] == "number"
    assert "thinking" not in captured  # incompatible with forced tool choice
    get_settings.cache_clear()
    assert llm.structured_output_mode("anthropic") == "native"
