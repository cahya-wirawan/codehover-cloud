"""OpenAI and Gemini batches (07 "optional submit_batch/get_batch", T24), against `httpx.MockTransport`.

Each adapter must submit the same request bodies its synchronous calls send, report whether a failed
submission could have reached the provider (`batch_not_sent` releases the holds), read each answer like a
synchronous response, attach only its own requests, and treat what an unfinished batch never ran as an
unbilled error.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from app.adapters.models import gemini, openai
from app.engine.gateway import ProviderError, supports_batch
from tests.unit.test_model_adapters import ANSWER, gemini_ok, openai_ok, request

Handler = Callable[[httpx.Request], httpx.Response]


def run(
    make: Callable[[httpx.AsyncClient], Any], handler: Handler, call: Callable[[Any], Awaitable[Any]]
) -> Any:
    async def go() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await call(make(http))

    return asyncio.run(go())


def openai_gw(http: httpx.AsyncClient) -> openai.OpenAIGateway:
    return openai.OpenAIGateway("sk-test", client=http)


def gemini_gw(http: httpx.AsyncClient) -> gemini.GeminiGateway:
    return gemini.GeminiGateway("AIza-test", client=http)


def test_both_new_gateways_offer_batches() -> None:
    client = httpx.AsyncClient()
    assert supports_batch(openai_gw(client)) and supports_batch(gemini_gw(client))


# ---------------------------------------------------------------- OpenAI
def test_openai_uploads_one_jsonl_file_of_responses_requests_and_creates_a_24h_batch() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/v1/files":
            return httpx.Response(200, json={"id": "file-1", "object": "file", "purpose": "batch"})
        return httpx.Response(200, json={"id": "batch_1", "status": "validating"})

    items = [("a1", request("openai", "medium")), ("a2", request("openai"))]
    assert run(openai_gw, handler, lambda gw: gw.submit_batch(items)) == "batch_1"
    upload, create = seen
    assert (
        upload.url == "https://api.openai.com/v1/files"
        and upload.headers["authorization"] == "Bearer sk-test"
    )
    form = upload.content.decode()
    assert 'name="purpose"' in form and "batch" in form and 'filename="requests.jsonl"' in form
    lines = [json.loads(line) for line in form.replace("\r\n", "\n").split("\n") if line.startswith("{")]
    assert [(line["custom_id"], line["method"], line["url"]) for line in lines] == [
        ("a1", "POST", "/v1/responses"),
        ("a2", "POST", "/v1/responses"),
    ]
    assert lines[0]["body"] == openai_gw(httpx.AsyncClient()).body(items[0][1])  # the synchronous call's body
    assert json.loads(create.content) == {
        "input_file_id": "file-1",
        "endpoint": "/v1/responses",
        "completion_window": "24h",
    }


@pytest.mark.parametrize(
    ("failing", "status", "code"),
    [
        ("/v1/files", 500, "batch_not_sent"),  # no batch exists before the create call
        ("/v1/batches", 400, "batch_not_sent"),  # refused: not created
        ("/v1/batches", 503, "batch_submit_failed"),  # may have been created: unknown
    ],
)
def test_openai_reports_whether_a_failed_submission_could_have_reached_the_provider(
    failing: str, status: int, code: str
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == failing:
            return httpx.Response(status, json={"error": {"message": "x"}})
        return httpx.Response(200, json={"id": "file-1"})

    with pytest.raises(ProviderError) as caught:
        run(openai_gw, handler, lambda gw: gw.submit_batch([("a1", request("openai"))]))
    assert caught.value.code == code


def test_openai_batch_states() -> None:
    for status, ended in (
        ("validating", False),
        ("in_progress", False),
        ("finalizing", False),
        ("completed", True),
        ("expired", True),
        ("failed", True),
        ("cancelled", True),
    ):
        handler = lambda _req, s=status: httpx.Response(200, json={"id": "batch_1", "status": s})  # noqa: E731
        assert run(openai_gw, handler, lambda gw: gw.batch_ended("batch_1")) is ended


def test_openai_results_are_read_like_synchronous_answers_and_unfinished_requests_are_errors() -> None:
    output = "\n".join(
        json.dumps(line)
        for line in [
            {
                "custom_id": "a2",
                "response": {"status_code": 200, "request_id": "req_2", "body": openai_ok()},
                "error": None,
            },
            {
                "custom_id": "zz",
                "response": {"status_code": 200, "body": openai_ok()},
                "error": None,
            },  # not ours
            {
                "custom_id": "a3",
                "response": {
                    "status_code": 200,
                    "body": openai_ok(
                        status="incomplete", incomplete_details={"reason": "max_output_tokens"}
                    ),
                },
            },
        ]
    )
    errors = json.dumps(
        {"custom_id": "a1", "response": None, "error": {"code": "batch_expired", "message": "x"}}
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/batches/batch_1":
            return httpx.Response(
                200,
                json={
                    "id": "batch_1",
                    "status": "expired",
                    "output_file_id": "file-out",
                    "error_file_id": "file-err",
                },
            )
        return httpx.Response(200, text=output if "file-out" in req.url.path else errors)

    requests = {cid: request("openai") for cid in ("a1", "a2", "a3", "a4")}
    items = {
        i.custom_id: i for i in run(openai_gw, handler, lambda gw: gw.batch_results("batch_1", requests))
    }
    assert set(items) == {"a1", "a2", "a3", "a4"}
    assert items["a2"].result is not None and items["a2"].result.output == ANSWER
    assert (
        items["a2"].result.provider_request_id == "req_2"
        and items["a2"].result.usage.cache_read_tokens == 400
    )
    assert items["a3"].error is not None and items["a3"].error.code == "output_truncated"  # billed, invalid
    assert items["a1"].error is not None and items["a1"].error.code == "batch_expired"
    assert (
        items["a4"].error is not None and items["a4"].error.code == "batch_expired"
    )  # the expired batch never ran it


def test_openai_a_completed_batch_that_omits_a_request_leaves_it_unanswered() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/batches/batch_1":
            return httpx.Response(
                200,
                json={
                    "id": "batch_1",
                    "status": "completed",
                    "output_file_id": "file-out",
                    "error_file_id": None,
                },
            )
        return httpx.Response(
            200, text=json.dumps({"custom_id": "a1", "response": {"status_code": 200, "body": openai_ok()}})
        )

    items = run(
        openai_gw,
        handler,
        lambda gw: gw.batch_results("batch_1", {"a1": request("openai"), "a2": request("openai")}),
    )
    assert [i.custom_id for i in items] == ["a1"]  # a2 stays unknown and keeps its hold


# ---------------------------------------------------------------- Gemini
def test_gemini_submits_inline_requests_keyed_by_attempt() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"name": "batches/123", "metadata": {"state": "JOB_STATE_PENDING"}})

    items = [("a1", request("gemini", "medium")), ("a2", request("gemini"))]
    assert run(gemini_gw, handler, lambda gw: gw.submit_batch(items)) == "batches/123"
    (sent,) = seen
    assert (
        str(sent.url)
        == "https://generativelanguage.googleapis.com/v1beta/models/test-model:batchGenerateContent"
    )
    assert sent.headers["x-goog-api-key"] == "AIza-test" and "key=" not in str(sent.url)
    batch = json.loads(sent.content)["batch"]
    assert batch["model"] == "models/test-model"
    inline = batch["inputConfig"]["requests"]["requests"]
    assert [r["metadata"]["key"] for r in inline] == ["a1", "a2"]
    assert inline[0]["request"] == gemini_gw(httpx.AsyncClient()).body(
        items[0][1]
    )  # the synchronous call's body


def test_gemini_does_not_send_a_batch_over_the_inline_limit_or_accept_a_strange_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gemini, "MAX_INLINE_BYTES", 100)
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"name": "../evil"})

    with pytest.raises(ProviderError) as caught:
        run(gemini_gw, handler, lambda gw: gw.submit_batch([("a1", request("gemini"))]))
    assert caught.value.code == "batch_not_sent" and calls == []
    monkeypatch.setattr(gemini, "MAX_INLINE_BYTES", 19_000_000)
    with pytest.raises(ProviderError) as caught:
        run(gemini_gw, handler, lambda gw: gw.submit_batch([("a1", request("gemini"))]))
    assert caught.value.code == "batch_submit_failed"  # sent, but no usable batch name came back


def test_gemini_batch_states_wherever_the_api_reports_them() -> None:
    cases = [
        ({"name": "batches/1", "metadata": {"state": "JOB_STATE_RUNNING"}}, False),
        ({"name": "batches/1", "metadata": {"state": "JOB_STATE_SUCCEEDED"}}, True),
        ({"name": "batches/1", "state": "JOB_STATE_EXPIRED"}, True),
        ({"name": "batches/1", "done": True}, True),
        ({"name": "batches/1", "state": "JOB_STATE_PENDING"}, False),
    ]
    for body, ended in cases:
        handler = lambda _req, b=body: httpx.Response(200, json=b)  # noqa: E731
        assert run(gemini_gw, handler, lambda gw: gw.batch_ended("batches/1")) is ended


def test_gemini_results_by_key_or_position_and_unfinished_jobs_are_errors() -> None:
    keyed = {
        "name": "batches/1",
        "done": True,
        "metadata": {"state": "JOB_STATE_SUCCEEDED"},
        "response": {
            "inlinedResponses": {
                "inlinedResponses": [
                    {"response": gemini_ok(reason="MAX_TOKENS"), "metadata": {"key": "a2"}},
                    {"response": gemini_ok(), "metadata": {"key": "a1"}},
                    {"response": gemini_ok(), "metadata": {"key": "zz"}},  # not ours
                ]
            }
        },
    }
    requests = {"a1": request("gemini"), "a2": request("gemini")}
    items = {
        i.custom_id: i
        for i in run(
            gemini_gw,
            lambda _r: httpx.Response(200, json=keyed),
            lambda gw: gw.batch_results("batches/1", requests),
        )
    }
    assert set(items) == {"a1", "a2"}
    assert items["a1"].result is not None and items["a1"].result.output == ANSWER
    assert items["a2"].error is not None and items["a2"].error.code == "output_truncated"

    positional = {
        "name": "batches/1",
        "state": "JOB_STATE_SUCCEEDED",
        "output": {
            "inlinedResponses": {
                "inlinedResponses": [{"response": gemini_ok()}, {"error": {"code": 3, "message": "bad"}}]
            }
        },
    }
    items = {
        i.custom_id: i
        for i in run(
            gemini_gw,
            lambda _r: httpx.Response(200, json=positional),
            lambda gw: gw.batch_results("batches/1", requests),
        )
    }
    assert (
        items["a1"].result is not None
        and items["a2"].error is not None
        and items["a2"].error.code == "batch_request_failed"
    )

    failed = {"name": "batches/1", "metadata": {"state": "JOB_STATE_FAILED"}}
    items = {
        i.custom_id: i
        for i in run(
            gemini_gw,
            lambda _r: httpx.Response(200, json=failed),
            lambda gw: gw.batch_results("batches/1", requests),
        )
    }
    assert {cid: i.error.code for cid, i in items.items() if i.error} == {
        "a1": "batch_failed",
        "a2": "batch_failed",
    }
