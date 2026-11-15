"""Small DeepSeek adapter with explicit protocol, usage and retry boundaries."""

import hashlib
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from .agent import ToolCallingAgent
from .budget import BudgetError, BudgetLedger
from .contracts import CURRENT_CALL
from .failures import ToolExecutionError
from .invocations import InvocationConflictError, InvocationLedger
from .runtime import RunInterrupted
from .trace import TraceLog

MODEL = "deepseek-flash"
ENDPOINT = "https://api.deepseek.com"


class ProviderError(RunInterrupted):
    def __init__(self, code: str, *, retryable=False):
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)


def load_credentials(path: Path | None = None) -> Credentials:
    value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not value:
        try:
            text = (path or Path.cwd().parent / "pico-api.txt").read_text(
                encoding="utf-8-sig"
            )
        except OSError:
            raise ProviderError("credentials_missing") from None
        keys = set(re.findall(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}", text))
        if len(keys) != 1:
            raise ProviderError("credentials_ambiguous")
        value = keys.pop()
    if not re.fullmatch(r"sk-[A-Za-z0-9_-]{16,}", value):
        raise ProviderError("credentials_invalid")
    return Credentials(value)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DeepSeekClient:
    def __init__(
        self,
        credentials: Credentials,
        ledger: BudgetLedger,
        *,
        transport=None,
        timeout=20,
        max_tokens=768,
        max_attempts=2,
        sleeper=time.sleep,
        max_input_bytes=24000,
    ):
        self.credentials = credentials
        self.ledger = ledger
        self.transport = transport or self._http
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.max_attempts = max_attempts
        self.sleeper = sleeper
        self.max_input_bytes = max_input_bytes
        self._recovery_checked = False

    def _http(self, payload, timeout):
        request = urllib.request.Request(
            ENDPOINT + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + self.credentials.api_key,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(
                request, timeout=timeout
            ) as response:
                data = response.read(131073)
            if len(data) > 131072:
                raise ProviderError("provider_response_limit")
            return json.loads(data)
        except urllib.error.HTTPError as exc:
            codes = {
                401: "authentication_failed",
                402: "insufficient_balance",
                429: "rate_limited",
            }
            raise ProviderError(
                codes.get(exc.code, "provider_http_error"),
                # A definite rate-limit response can be retried. A server error
                # may arrive after processing, so treat it as an uncertain
                # result and never issue a second paid request automatically.
                retryable=exc.code == 429,
            ) from None
        except (OSError, urllib.error.URLError):
            raise ProviderError("provider_connection_error") from None
        except ValueError:
            raise ProviderError("provider_invalid_json") from None

    def probe(self):
        result = {}
        for route in ("models", "user/balance"):
            request = urllib.request.Request(
                ENDPOINT + "/" + route,
                headers={"Authorization": "Bearer " + self.credentials.api_key},
            )
            try:
                with urllib.request.build_opener(NoRedirect()).open(
                    request, timeout=self.timeout
                ) as response:
                    data = json.loads(response.read(131072))
                if route == "models":
                    result["models"] = [item["id"] for item in data["data"]]
                else:
                    result["balance_available"] = data["is_available"]
                    result["balances"] = [
                        {
                            "currency": item["currency"],
                            "total_balance": item["total_balance"],
                        }
                        for item in data["balance_infos"]
                    ]
            except (OSError, ValueError, KeyError):
                raise ProviderError("provider_probe_failed") from None
        return result

    def _bounded_transport(self, payload, timeout):
        mailbox = queue.Queue(maxsize=1)

        def invoke():
            try:
                mailbox.put((True, self.transport(payload, timeout)))
            except ProviderError as exc:
                mailbox.put((False, exc))
            except Exception:  # noqa: BLE001 - never expose raw transport errors or credentials
                mailbox.put((False, ProviderError("provider_connection_error")))

        threading.Thread(target=invoke, daemon=True).start()
        try:
            success, result = mailbox.get(timeout=timeout)
        except queue.Empty:
            # The provider may have accepted and completed the request even
            # though the local deadline elapsed. Preserve UNKNOWN and do not
            # replay a potentially billable request automatically.
            raise ProviderError("provider_timeout") from None
        if not success:
            raise result
        return result

    def complete(
        self,
        messages: list[dict],
        *,
        task_id: UUID,
        trace: TraceLog,
        tools=None,
        max_tokens=None,
        deadline=None,
        response_json=False,
    ):
        limit = min(max_tokens or self.max_tokens, self.max_tokens)
        payload = {
            "model": MODEL,
            "messages": json.loads(json.dumps(messages)),
            "max_tokens": limit,
            "temperature": 0,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_json:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload, ensure_ascii=False).encode()
        if len(body) > self.max_input_bytes or len(messages) > 32:
            raise ProviderError("provider_context_limit")
        accounting = InvocationLedger(trace.database, task_id)
        request_sha = hashlib.sha256(body).hexdigest()
        try:
            replay = (
                accounting.reusable_model_response(MODEL, "deepseek", request_sha)
                if not self._recovery_checked
                else None
            )
            self._recovery_checked = True
        except InvocationConflictError:
            raise ProviderError("provider_replay_evidence_invalid") from None
        if replay is not None:
            trace.emit(
                "model.response_reused",
                {
                    "provider": "deepseek",
                    "model": MODEL,
                    "prompt_sha256": request_sha,
                    "response_sha256": hashlib.sha256(
                        json.dumps(replay, sort_keys=True).encode()
                    ).hexdigest(),
                },
            )
            return replay
        standalone = accounting.active_run_id() is None
        identity = accounting.start(
            MODEL,
            "model",
            "deepseek",
            request_sha,
            standalone=standalone,
        )
        token = CURRENT_CALL.set(identity)
        state = "FAILED"
        try:
            result = self._complete_recorded(
                payload, body, limit, task_id, trace, deadline, accounting, identity
            )
            state = "SUCCEEDED"
            return result
        finally:
            current = CURRENT_CALL.get()
            try:
                # Also converge failures in budget/trace/storage hooks before or after transport.
                accounting._record_exception(
                    current,
                    ToolExecutionError(
                        "provider_accounting_interrupted", outcome_unknown=True
                    ),
                )
                if standalone:
                    accounting.close_provider_run(identity, state)
            finally:
                CURRENT_CALL.reset(token)

    def _complete_recorded(
        self, payload, body, limit, task_id, trace, deadline, accounting, identity
    ):
        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1:
                identity = accounting.next_model_attempt(identity)
                CURRENT_CALL.set(identity)
            if deadline is not None and time.monotonic() >= deadline:
                accounting.finish(identity, "FAILED", "task_timeout")
                raise ProviderError("task_timeout")
            attempt_id = identity.attempt_id
            try:
                self.ledger.reserve(attempt_id, task_id, len(body) + 2048, limit)
            except BudgetError:
                accounting.finish(identity, "FAILED", "provider_budget_exhausted")
                raise ProviderError("provider_budget_exhausted") from None
            trace.emit(
                "model.request",
                {
                    "attempt_id": str(attempt_id),
                    "provider": "deepseek",
                    "model": MODEL,
                    "prompt_sha256": hashlib.sha256(body).hexdigest(),
                    "input_bytes": len(body),
                    "max_output_tokens": limit,
                },
            )
            started = time.monotonic()
            try:
                timeout = (
                    min(self.timeout, max(0.01, deadline - time.monotonic()))
                    if deadline is not None
                    else self.timeout
                )
                response = self._bounded_transport(payload, timeout)
                usage = response["usage"]
                if (
                    type(usage["prompt_tokens"]) is not int
                    or type(usage["completion_tokens"]) is not int
                    or usage["completion_tokens"] > limit
                ):
                    raise ProviderError("provider_invalid_usage")
                try:
                    cost = self.ledger.settle(
                        attempt_id, usage["prompt_tokens"], usage["completion_tokens"]
                    )
                except (BudgetError, ValueError):
                    raise ProviderError("provider_invalid_usage") from None
                choice = response["choices"][0]
                finish = choice["finish_reason"]
                message = choice["message"]
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    raise ProviderError("provider_protocol_error")
                response_hash = hashlib.sha256(
                    json.dumps(message, sort_keys=True).encode()
                ).hexdigest()
                trace.emit(
                    "model.result",
                    {
                        "attempt_id": str(attempt_id),
                        "provider": "deepseek",
                        "model": response.get("model", MODEL),
                        "response_sha256": response_hash,
                        "prompt_tokens": usage["prompt_tokens"],
                        "completion_tokens": usage["completion_tokens"],
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "cost_upper_cny": cost,
                        "finish_reason": finish,
                    },
                )
                if finish not in {"stop", "tool_calls"}:
                    raise ProviderError(
                        "provider_output_truncated"
                        if finish == "length"
                        else "provider_protocol_error"
                    )
                accounting.finish(
                    identity,
                    "SUCCEEDED",
                    "provider_response_received",
                    json.dumps(message, ensure_ascii=False),
                    artifact_kind="model_response",
                )
                return message
            except (KeyError, TypeError, IndexError, ValueError):
                error = ProviderError("provider_protocol_error")
            except ProviderError as exc:
                error = exc
            accounting.finish(
                identity,
                "UNKNOWN"
                if error.code
                in {
                    "provider_timeout",
                    "provider_connection_error",
                    "provider_http_error",
                }
                else "FAILED",
                error.code,
            )
            trace.emit(
                "model.error",
                {
                    "attempt_id": str(attempt_id),
                    "provider": "deepseek",
                    "code": error.code,
                    "retryable": error.retryable,
                    "attempt": attempt,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                },
            )
            if (
                error.code != "rate_limited"
                or not error.retryable
                or attempt == self.max_attempts
            ):
                raise error
            delay = min(0.25 * 2 ** (attempt - 1), 1)
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise ProviderError("task_timeout")
            self.sleeper(delay)
        raise ProviderError("provider_protocol_error")


class DeepSeekAdapter(ToolCallingAgent):
    """Compatibility entry point; conversation state now belongs to the generic agent."""

    def __init__(self, client, **kwargs):
        super().__init__(
            client,
            model_identity="deepseek:" + MODEL,
            error_type=ProviderError,
            **kwargs,
        )
