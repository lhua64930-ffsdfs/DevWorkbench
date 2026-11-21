"""Reserve provider token capacity before sending evaluation requests."""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

import httpx
from sqlalchemy import update

from app.db.models import EvalTokenBudget
from app.db.session import SessionLocal


class TokenBudgetExceeded(RuntimeError):
    pass


class TokenBudget:
    def __init__(self, eval_id: UUID):
        self.eval_id = eval_id

    def reserve(self, amount: int) -> None:
        with SessionLocal.begin() as db:
            accepted = db.execute(update(EvalTokenBudget).where(
                EvalTokenBudget.eval_id == self.eval_id,
                EvalTokenBudget.blocked.is_(False),
                EvalTokenBudget.committed_tokens + amount <= EvalTokenBudget.token_limit,
            ).values(committed_tokens=EvalTokenBudget.committed_tokens + amount)).rowcount
            if not accepted:
                db.execute(update(EvalTokenBudget).where(EvalTokenBudget.eval_id == self.eval_id).values(blocked=True))
        if not accepted:
            raise TokenBudgetExceeded("Token 预算不足，已拦截模型请求")

    def settle(self, reserved: int, actual: int) -> None:
        with SessionLocal.begin() as db:
            db.execute(update(EvalTokenBudget).where(EvalTokenBudget.eval_id == self.eval_id).values(
                committed_tokens=EvalTokenBudget.committed_tokens + actual - reserved,
                reported_tokens=EvalTokenBudget.reported_tokens + actual,
            ))
            if actual > reserved:
                db.execute(update(EvalTokenBudget).where(EvalTokenBudget.eval_id == self.eval_id).values(blocked=True))

    def summary(self) -> dict:
        with SessionLocal() as db:
            row = db.get(EvalTokenBudget, self.eval_id)
            return {
                "limit": row.token_limit, "committed": row.committed_tokens,
                "reported": row.reported_tokens, "blocked": row.blocked,
            }


_current: ContextVar[TokenBudget | None] = ContextVar("eval_token_budget", default=None)


@contextmanager
def evaluation_budget(budget: TokenBudget | None):
    token = _current.set(budget)
    try:
        yield
    finally:
        _current.reset(token)


def budget_active() -> bool:
    return _current.get() is not None


def _reserve(request: httpx.Request) -> None:
    budget = _current.get()
    if budget is None:
        return
    payload = json.loads(request.content)
    path = request.url.path.rstrip("/")
    if request.method != "POST" or payload.get("stream") or not path.endswith(("/chat/completions", "/embeddings", "/reranks", "/text-rerank/text-rerank")):
        raise TokenBudgetExceeded("Token 预算模式仅允许受控的非流式模型调用")
    ceiling = 0
    if path.endswith("/chat/completions"):
        ceiling = payload.get("max_completion_tokens") or payload.get("max_tokens")
        if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 1 or payload.get("n", 1) != 1:
            raise TokenBudgetExceeded("模型请求需要明确的单回答输出上限")
    # Account for all request fields, tool definitions and provider framing.
    # Provider usage supersedes this conservative reservation when available.
    framing = 512 + 64 * len(payload.get("messages") or ())
    reserved = len(request.content) + framing + ceiling
    budget.reserve(reserved)
    request.extensions["eval_token_reservation"] = (budget, reserved)


def _settle(response: httpx.Response) -> None:
    reservation = response.request.extensions.get("eval_token_reservation")
    if reservation is None or not response.is_success:
        return
    try:
        usage = response.json().get("usage") or {}
    except (ValueError, AttributeError):
        return
    tokens = usage.get("total_tokens")
    if tokens is None and isinstance(usage.get("prompt_tokens"), int):
        tokens = usage["prompt_tokens"] + (usage.get("completion_tokens") or 0)
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0:
        budget, reserved = reservation
        budget.settle(reserved, tokens)


async def _reserve_async(request: httpx.Request) -> None:
    _reserve(request)


async def _settle_async(response: httpx.Response) -> None:
    if response.request.extensions.get("eval_token_reservation"):
        await response.aread()
        _settle(response)


def _settle_sync(response: httpx.Response) -> None:
    if response.request.extensions.get("eval_token_reservation"):
        response.read()
        _settle(response)


def model_async_client(**kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(event_hooks={"request": [_reserve_async], "response": [_settle_async]}, **kwargs)


def model_sync_client(**kwargs) -> httpx.Client:
    return httpx.Client(event_hooks={"request": [_reserve], "response": [_settle_sync]}, **kwargs)
