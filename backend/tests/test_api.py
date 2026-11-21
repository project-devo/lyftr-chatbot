"""API contract tests. The provider is mocked via LiteLLM's mock_response, so the real
LiteLLM disk cache and our key-failover loop are exercised without network calls."""
import os
import tempfile
import uuid

TMP = tempfile.mkdtemp()
os.environ.update(
    LLM_API_KEYS="test-key-1,test-key-2",
    LLM_MODEL="gemini/test-model",
    DATABASE_URL=f"sqlite:///{TMP}/test.db",
    CACHE_DIR=f"{TMP}/cache",
    ENABLE_FAILURE_SIMULATION="true",
    LITELLM_LOCAL_MODEL_COST_MAP="True",
)

import litellm  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

REAL_COMPLETION = litellm.completion


class FakeProvider:
    def __init__(self):
        self.calls = []  # (api_key, messages) per attempt that reached LiteLLM
        self.fail = {}  # api_key -> exception raised by the "provider"

    def __call__(self, **kw):
        self.calls.append((kw["api_key"], kw["messages"]))
        if kw["api_key"] in self.fail:
            raise self.fail[kw["api_key"]]
        text = f"echo: {kw['messages'][-1]['content']}"
        prompt = sum(len(m["content"].split()) for m in kw["messages"])
        return REAL_COMPLETION(**kw, mock_response={
            "model": kw["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": len(text.split()), "total_tokens": prompt + len(text.split())},
        })


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def fake(monkeypatch):
    f = FakeProvider()
    monkeypatch.setattr(litellm, "completion", f)
    return f


def sid():
    return "s-" + uuid.uuid4().hex[:10]


def msg(text="hello"):
    return f"{text} {uuid.uuid4().hex[:8]}"  # unique so tests never share cache entries


def chat(client, session_id, message, headers=None, **extra):
    return client.post("/chat", json={"session_id": session_id, "message": message, **extra}, headers=headers or {})


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "keys_configured": 2, "cache_enabled": True}


@pytest.mark.parametrize("body", [
    {},
    {"session_id": "s-1"},
    {"message": "hi"},
    {"session_id": "", "message": "hi"},
    {"session_id": "x" * 129, "message": "hi"},
    {"session_id": "s-1", "message": ""},
    {"session_id": "s-1", "message": "   \n\t "},
    {"session_id": "s-1", "message": "a" * 4001},
    {"session_id": "s-1", "message": 123},
])
def test_422_validation(client, fake, body):
    r = client.post("/chat", json=body)
    assert r.status_code == 422
    assert "detail" in r.json()
    assert fake.calls == []


def test_422_malformed_json(client, fake):
    r = client.post("/chat", content=b'{"session_id": "s-1", "message": ', headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and "detail" in r.json()


def test_422_malformed_simulation_header(client, fake):
    r = chat(client, sid(), msg(), headers={"X-Simulate-Failure": "key_1=418"})
    assert r.status_code == 422 and "detail" in r.json()
    assert fake.calls == []


def test_message_limit_applies_after_trimming(client, fake):
    r = chat(client, sid(), "  " + "a" * 4000 + "  ")
    assert r.status_code == 200


def test_404_unknown_session(client):
    r = client.get("/sessions/does-not-exist/messages")
    assert r.status_code == 404
    assert r.json() == {"detail": "session not found"}


def test_chat_response_contract(client, fake):
    s, m = sid(), msg()
    r = chat(client, s, m)
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == s
    assert body["reply"] == f"echo: {m}"
    meta = body["metadata"]
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert meta["model"] == "gemini/test-model"
    assert meta["key_id"] == "key_1" and meta["fallback_attempts"] == 0
    for field in ("latency_ms", "input_tokens", "output_tokens"):
        assert isinstance(meta[field], int) and meta[field] >= 0
    assert meta["input_tokens"] > 0 and meta["output_tokens"] > 0


def test_cache_hit_labelling(client, fake):
    m = msg("capital of France?")
    first = chat(client, sid(), m).json()
    second = chat(client, sid(), m).json()
    other = chat(client, sid(), msg("capital of Spain?")).json()

    assert first["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert second["metadata"]["resolution_source"] == "LLM_CACHE"
    assert second["reply"] == first["reply"]
    assert second["metadata"]["key_id"] is None and second["metadata"]["fallback_attempts"] == 0
    assert second["metadata"]["input_tokens"] == first["metadata"]["input_tokens"]
    assert second["metadata"]["output_tokens"] == first["metadata"]["output_tokens"]
    assert other["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_cache_key_ignores_api_key(client, fake):
    m = msg()
    via_key2 = chat(client, sid(), m, headers={"X-Simulate-Failure": "key_1=429"}).json()
    assert via_key2["metadata"]["key_id"] == "key_2"
    retry = chat(client, sid(), m).json()  # key_1 now healthy
    assert retry["metadata"]["resolution_source"] == "LLM_CACHE"
    assert retry["reply"] == via_key2["reply"]


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_simulated_failover_to_key_2(client, fake, status):
    r = chat(client, sid(), msg(), headers={"X-Simulate-Failure": f"key_1={status}"})
    assert r.status_code == 200
    meta = r.json()["metadata"]
    assert meta["key_id"] == "key_2" and meta["fallback_attempts"] == 1
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert r.json()["reply"].startswith("echo:")
    assert [k for k, _ in fake.calls] == ["test-key-2"]  # key_1 never reached the provider


def test_simulated_failover_not_masked_by_cache(client, fake):
    s, m = sid(), msg()
    chat(client, s, m)  # now cached
    r = chat(client, sid(), m, headers={"X-Simulate-Failure": "key_1=500"}).json()
    assert r["metadata"]["key_id"] == "key_2" and r["metadata"]["fallback_attempts"] == 1


def test_all_keys_fail_returns_503_and_writes_nothing(client, fake):
    s = sid()
    ok = chat(client, s, msg())
    assert ok.status_code == 200
    r = chat(client, s, msg(), headers={"X-Simulate-Failure": "*=429"})
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}
    assert len(client.get(f"/sessions/{s}/messages").json()["messages"]) == 2

    fresh = sid()
    assert chat(client, fresh, msg(), headers={"X-Simulate-Failure": "key_1=429,key_2=500"}).status_code == 503
    assert client.get(f"/sessions/{fresh}/messages").status_code == 404


def test_real_provider_errors_fail_over(client, fake):
    fake.fail["test-key-1"] = litellm.RateLimitError("429 RESOURCE_EXHAUSTED: quota exceeded", "gemini", "test")
    r = chat(client, sid(), msg()).json()
    assert r["metadata"]["key_id"] == "key_2" and r["metadata"]["fallback_attempts"] == 1

    fake.fail["test-key-2"] = litellm.InternalServerError("boom", "gemini", "test")
    r = chat(client, sid(), msg())
    assert r.status_code == 503 and r.json() == {"detail": "all providers unavailable"}


def test_non_retryable_error_is_clean_json(client, fake):
    s = sid()
    fake.fail["test-key-1"] = litellm.BadRequestError("bad request", "test", "gemini")
    r = chat(client, s, msg())
    assert r.status_code == 400 and "detail" in r.json()
    assert [k for k, _ in fake.calls] == ["test-key-1"]  # no pointless failover
    assert client.get(f"/sessions/{s}/messages").status_code == 404


def test_simulation_header_ignored_when_disabled(client, fake, monkeypatch):
    monkeypatch.setattr(main, "SIMULATION_ENABLED", False)
    r = chat(client, sid(), msg(), headers={"X-Simulate-Failure": "*=429"})
    assert r.status_code == 200 and r.json()["metadata"]["key_id"] == "key_1"


def test_multi_turn_history_order_and_separation(client, fake):
    a, b = sid(), sid()
    chat(client, a, "My name is Priyank")
    chat(client, b, "I like tea")
    chat(client, a, "What is my name?")

    sent = fake.calls[-1][1]  # messages sent to the LLM on the last turn of session a
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[1]["content"] == "My name is Priyank" and sent[3]["content"] == "What is my name?"
    assert all("tea" not in m["content"] for m in sent)

    hist = client.get(f"/sessions/{a}/messages").json()
    assert hist["session_id"] == a
    assert [(m["role"], m["content"]) for m in hist["messages"]] == [
        ("user", "My name is Priyank"),
        ("assistant", "echo: My name is Priyank"),
        ("user", "What is my name?"),
        ("assistant", "echo: What is my name?"),
    ]
    assert "metadata" not in hist["messages"][0]
    meta = hist["messages"][1]["metadata"]
    assert meta["resolution_source"] in ("LIVE_INFERENCE", "LLM_CACHE")
    assert isinstance(meta["input_tokens"], int) and isinstance(meta["output_tokens"], int)
    assert len(client.get(f"/sessions/{b}/messages").json()["messages"]) == 2


def test_tokens_are_per_request(client, fake):
    s1, s2 = sid(), sid()
    t1 = chat(client, s1, msg("alpha beta")).json()["metadata"]
    t2 = chat(client, s2, msg("gamma delta")).json()["metadata"]
    assert t1["input_tokens"] == t2["input_tokens"]  # equal-length first messages

    turn2 = chat(client, s1, msg("again")).json()["metadata"]
    expected = sum(len(m["content"].split()) for m in fake.calls[-1][1])
    assert turn2["input_tokens"] == expected  # exactly this request's prompt, not a running total
    assert turn2["output_tokens"] == len(f"echo: {fake.calls[-1][1][-1]['content']}".split())


def test_system_prompt_persists_for_session(client, fake):
    s = sid()
    chat(client, s, msg(), system_prompt="You are a pirate.")
    chat(client, s, msg())
    assert fake.calls[-1][1][0] == {"role": "system", "content": "You are a pirate."}
    hist = client.get(f"/sessions/{s}/messages").json()["messages"]
    assert all(m["role"] != "system" for m in hist)


def test_unhandled_error_is_json_500(fake, monkeypatch):
    monkeypatch.setattr(main.db, "history", lambda _: 1 / 0)
    with TestClient(main.app, raise_server_exceptions=False) as c:
        r = chat(c, sid(), msg())
    assert r.status_code == 500 and r.json() == {"detail": "internal error"}
