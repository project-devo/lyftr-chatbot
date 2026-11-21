import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints

from . import db, llm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

DEFAULT_SYSTEM_PROMPT = os.getenv("DEFAULT_SYSTEM_PROMPT", "You are a helpful, concise assistant.")
SIMULATION_ENABLED = os.getenv("ENABLE_FAILURE_SIMULATION", "false").strip().lower() == "true"
ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",") if o.strip()]


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    log.info("model=%s keys=%d simulation=%s origins=%s", llm.MODEL, len(llm.KEYS), SIMULATION_ENABLED, ALLOWED_ORIGINS)
    yield


app = FastAPI(title="LLM Chatbot with Key Failover and Response Caching", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Simulate-Failure"],
)


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.error("unhandled %s", type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    system_prompt: str | None = Field(default=None, max_length=8000)


@app.get("/health")
def health():
    try:
        db.ping()
    except Exception:
        raise HTTPException(503, "database unavailable")
    if not llm.KEYS:
        raise HTTPException(503, "no API keys configured")
    return {"status": "ok", "keys_configured": len(llm.KEYS), "cache_enabled": llm.litellm.cache is not None}


@app.post("/chat")
def chat(req: ChatRequest, x_simulate_failure: Annotated[str | None, Header()] = None):
    started, user_at = time.perf_counter(), db.now()
    try:
        simulate = llm.parse_simulation(x_simulate_failure) if SIMULATION_ENABLED else {}
    except ValueError:
        raise HTTPException(422, "malformed X-Simulate-Failure header (expected e.g. key_1=429,key_2=500 or *=429)")

    new_prompt = (req.system_prompt or "").strip() or None
    _, stored_prompt = db.get_system_prompt(req.session_id)
    system_prompt = new_prompt or stored_prompt or DEFAULT_SYSTEM_PROMPT
    messages = [
        {"role": "system", "content": system_prompt},
        *({"role": m["role"], "content": m["content"]} for m in db.history(req.session_id)),
        {"role": "user", "content": req.message},
    ]

    try:
        result = llm.complete(messages, simulate)
    except llm.AllKeysFailed:
        raise HTTPException(503, "all providers unavailable")
    except llm.LLMRejected as e:
        raise HTTPException(400, f"the model rejected the request ({e})")

    reply = result.pop("reply")
    metadata = {
        "resolution_source": result["resolution_source"],
        "model": result["model"],
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "key_id": result["key_id"],
        "fallback_attempts": result["fallback_attempts"],
    }
    db.save_turn(req.session_id, new_prompt, req.message, user_at, reply, metadata)
    return {"session_id": req.session_id, "reply": reply, "metadata": metadata}


@app.get("/sessions/{session_id}/messages")
def session_messages(session_id: str):
    exists, _ = db.get_system_prompt(session_id)
    if not exists:
        raise HTTPException(404, "session not found")
    return {"session_id": session_id, "messages": db.history(session_id)}
