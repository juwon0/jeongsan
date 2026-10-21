"""Reads receipt photos with a local Ollama model.

The request handling (JSON Schema output, a schema-in-the-prompt fallback for runtimes that refuse it, code-fence
trimming and one warmer retry) is adapted from Chumsak, my Week 2 project: https://github.com/juwon0/chumsak (MIT).
"""

import base64
import json
from typing import Protocol

import httpx

from .prompts import RECEIPT_PROMPT, SCHEMA_HINT
from .schemas import ReceiptRead

SCHEMA = ReceiptRead.model_json_schema(mode="serialization")


class ModelError(RuntimeError):
    """The local model was unreachable or answered with something unusable."""


class Reader(Protocol):
    model: str

    async def read(self, image: bytes) -> ReceiptRead: ...


class OllamaReader:
    """Calls a local Ollama server. Photos go to localhost and nowhere else."""

    def __init__(self, client: httpx.AsyncClient, model: str, num_ctx: int = 8192) -> None:
        self._client = client
        self.model = model
        self._num_ctx = num_ctx  # the prompt, the schema and one photo fit with room to spare
        self._schema_in_prompt = False  # set once the runtime has refused structured output

    async def read(self, image: bytes) -> ReceiptRead:
        message = {"role": "user", "content": RECEIPT_PROMPT, "images": [base64.b64encode(image).decode("ascii")]}
        payload = {
            "model": self.model,
            "messages": [message],
            "format": SCHEMA,  # constrained decoding to this JSON Schema where the runtime supports it
            "stream": False,
            "think": False,  # Gemma 4 thinks by default: slower, and a receipt doesn't need it
            # An answer is about 250 tokens; the cap ends a runaway loop long before the timeout.
            "options": {"temperature": 0, "num_ctx": self._num_ctx, "num_predict": 1024},
        }
        if self._schema_in_prompt:
            _schema_into_prompt(payload)
        # One retry for an unusable answer, a little warmer: at temperature 0 it would come back the same.
        for temperature in (0, 0.2):
            payload["options"]["temperature"] = temperature
            try:
                resp = await self._client.post("/api/chat", json=payload)
                if resp.status_code == 501 and "format" in payload:
                    # Homebrew's Ollama runs Gemma 4 on MLX without structured output and answers 501. Put the
                    # schema in the prompt instead, for this photo and the ones after it; validation below still
                    # rejects anything that doesn't fit.
                    self._schema_in_prompt = True
                    _schema_into_prompt(payload)
                    resp = await self._client.post("/api/chat", json=payload)
            except httpx.HTTPError as exc:
                raise ModelError(f"{self.model}: {exc!r}") from exc
            if resp.is_error:
                raise ModelError(f"{self.model}: HTTP {resp.status_code} {resp.text[:300]}")
            try:
                content = resp.json()["message"]["content"]
                # Without constrained decoding the model sometimes wraps its JSON in a ```json fence.
                start, end = content.find("{"), content.rfind("}")
                if start != -1 and end > start:
                    content = content[start : end + 1]
                data = json.loads(content)
                # Every field has a default, so an answer about something else would otherwise pass as empty.
                if not isinstance(data, dict) or not data.keys() & ReceiptRead.model_fields.keys():
                    raise ValueError(f"no receipt fields in {content[:100]!r}")
                return ReceiptRead.model_validate(data)
            except (KeyError, TypeError, ValueError) as exc:
                unusable = exc
        raise ModelError(f"{self.model}: unusable answer: {unusable}") from unusable


def _schema_into_prompt(payload: dict) -> None:
    schema_json = json.dumps(payload.pop("format"), ensure_ascii=False)
    payload["messages"][0]["content"] += SCHEMA_HINT.format(schema=schema_json)
