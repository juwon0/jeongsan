import io
from datetime import date

import pytest
from PIL import Image

from app.schemas import Receipt, ReceiptRead

TODAY = date(2026, 10, 20)

CAFE = {
    "is_receipt": True,
    "merchant": "달빛커피 역삼점",
    "business_number": "123-45-67891",
    "date": "2026-10-14",
    "time": "08:42",
    "currency": "KRW",
    "total": 9000,
    "breakdown": [{"label": "공급가액", "amount": 8182}, {"label": "부가세", "amount": 818}],
    "summary": "아메리카노 2잔",
    "category": "cafe",
    "payment": "card",
    "unreadable": [],
}


class FakeReader:
    """Answers in order (the last answer repeats) and remembers every image it was shown."""

    model = "fake-model"

    def __init__(self, *answers: dict | Exception) -> None:
        self.answers = list(answers) or [CAFE]
        self.images: list[bytes] = []

    async def read(self, image: bytes) -> ReceiptRead:
        self.images.append(image)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return ReceiptRead.model_validate(answer)


def photo(size: tuple[int, int] = (60, 40), color: str = "white", fmt: str = "JPEG", **save) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA" if fmt == "PNG" else "RGB", size, color).save(buf, fmt, **save)
    return buf.getvalue()


def row(**fields) -> Receipt:
    """A clean card-slip row; override what the test is about."""
    return Receipt.model_validate({"id": "0" * 16, "file_name": "a.jpg", **CAFE, **fields})


@pytest.fixture
def make_photo():
    return photo


@pytest.fixture
def make_row():
    return row


@pytest.fixture
def fake_reader():
    return FakeReader
