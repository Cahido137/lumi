from types import SimpleNamespace

import app.core.llm as llm


def test_ollama_provider_uses_json_mode(monkeypatch):
    monkeypatch.setattr(
        llm, "get_llmsettings", lambda: SimpleNamespace(llm_provider="ollama", llm_base_url="http://127.0.0.1:11434/v1")
    )
    assert llm.get_planner_structured_method() == "json_mode"


def test_ollama_base_url_keyword_uses_json_mode(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_llmsettings",
        lambda: SimpleNamespace(llm_provider="", llm_base_url="http://127.0.0.1:11434/ollama/v1"),
    )
    assert llm.get_planner_structured_method() == "json_mode"


def test_other_providers_use_function_calling(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_llmsettings",
        lambda: SimpleNamespace(llm_provider="deepseek", llm_base_url="https://api.deepseek.com/v1"),
    )
    assert llm.get_planner_structured_method() == "function_calling"
