import base64
import json

import httpx
import pytest

from app.model import SCHEMA, ModelError, OllamaReader
from app.schemas import ReceiptRead
from conftest import CAFE

PHOTO = b"\xff\xd8\xffphoto"


def ollama(handler):
    return httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler))


def reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})


@pytest.mark.anyio
async def test_read_sends_the_photo_with_a_schema_and_no_thinking():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return reply(json.dumps(CAFE, ensure_ascii=False))

    async with ollama(handler) as http:
        result = await OllamaReader(http, "gemma4:12b").read(PHOTO)

    assert result.total == 9000
    assert result.merchant == "달빛커피 역삼점"
    assert seen["model"] == "gemma4:12b"
    assert seen["stream"] is False
    assert seen["think"] is False
    assert seen["options"] == {"temperature": 0, "num_ctx": 8192, "num_predict": 1024}
    assert seen["format"] == SCHEMA
    assert base64.b64decode(seen["messages"][0]["images"][0]) == PHOTO


def test_the_schema_asks_for_every_field():
    assert set(SCHEMA["required"]) == set(ReceiptRead.model_fields)
    assert SCHEMA["properties"]["category"]["enum"] == ["meals", "cafe", "transport", "lodging", "supplies", "other"]


@pytest.mark.anyio
async def test_schema_goes_into_the_prompt_when_ollama_cannot_constrain_output():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if "format" in sent[-1]:  # what Homebrew's Ollama says when it runs Gemma 4 on MLX
            return httpx.Response(501, json={"error": "structured output is unavailable"})
        return reply(json.dumps(CAFE))

    async with ollama(handler) as http:
        result = await OllamaReader(http, "gemma4:12b").read(PHOTO)

    assert result.total == 9000
    assert len(sent) == 2
    assert "format" not in sent[1]
    assert '"business_number"' in sent[1]["messages"][0]["content"]
    assert sent[1]["messages"][0]["images"] == sent[0]["messages"][0]["images"]


@pytest.mark.anyio
async def test_after_one_501_the_next_photos_go_straight_to_the_schema_in_the_prompt():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if "format" in sent[-1]:
            return httpx.Response(501, json={"error": "structured output is unavailable"})
        return reply(json.dumps(CAFE))

    async with ollama(handler) as http:
        reader = OllamaReader(http, "gemma4:12b")
        await reader.read(PHOTO)
        await reader.read(PHOTO)

    assert len(sent) == 3  # the first photo costs a 501 and a retry, the second one request
    assert "format" not in sent[2]
    assert sent[2]["messages"][0]["content"] == sent[1]["messages"][0]["content"]


@pytest.mark.anyio
async def test_json_inside_a_code_fence_is_accepted():
    fenced = "```json\n" + json.dumps(CAFE, ensure_ascii=False) + "\n```"
    async with ollama(lambda request: reply(fenced)) as http:
        result = await OllamaReader(http, "gemma4:12b").read(PHOTO)

    assert result.summary == "아메리카노 2잔"


@pytest.mark.anyio
async def test_unusable_answer_is_retried_once_a_little_warmer():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return reply("I see a receipt." if len(sent) == 1 else json.dumps(CAFE))

    async with ollama(handler) as http:
        result = await OllamaReader(http, "gemma4:12b").read(PHOTO)

    assert result.total == 9000
    assert [body["options"]["temperature"] for body in sent] == [0, 0.2]


@pytest.mark.anyio
async def test_loose_answers_are_cleaned_up():
    # The shapes a model writes once the schema is only a hint in the prompt.
    loose = {
        "is_receipt": None,
        "merchant": "  온누리김밥\n선릉점 ",
        "date": "2026.10.14",
        "total": "₩8,500",
        "breakdown": [
            {"label": "공급가액", "amount": "7,727"},
            {"label": "부가세", "amount": "773원"},
            {"label": "합계", "amount": "8,500"},
            "공급가액 7,727",
        ],
        "category": "식대",
        "payment": "신용카드",
        "unreadable": ["금액", "nothing"],
    }
    async with ollama(lambda request: reply(json.dumps(loose, ensure_ascii=False))) as http:
        result = await OllamaReader(http, "gemma4:12b").read(PHOTO)

    assert result.is_receipt is True
    assert result.merchant == "온누리김밥 선릉점"
    assert result.total == 8500
    assert [(line.label, line.amount) for line in result.breakdown] == [("공급가액", 7727), ("부가세", 773)]
    assert result.category == "meals"
    assert result.payment == "card"
    assert result.unreadable == ["total"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        reply("not json"),
        reply(json.dumps({"answer": "This is a cat."})),
        httpx.Response(500, json={"error": "model not found"}),
    ],
)
async def test_unusable_or_failed_answers_raise_model_error(response):
    async with ollama(lambda request: response) as http:
        with pytest.raises(ModelError):
            await OllamaReader(http, "gemma4:12b").read(PHOTO)


@pytest.mark.anyio
async def test_unreachable_ollama_raises_model_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with ollama(handler) as http:
        with pytest.raises(ModelError, match="connection refused"):
            await OllamaReader(http, "gemma4:12b").read(PHOTO)
