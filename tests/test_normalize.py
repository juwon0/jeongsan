import pytest

from app import normalize as nz


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (9000, 9000.0),
        ("9,000원", 9000.0),
        ("₩ 9,000", 9000.0),
        ("$12.86", 12.86),
        ("12.86 USD", 12.86),
        ("-6,000", -6000.0),
        ("(1,000)", -1000.0),
        ("1.234.567", 1234567.0),
        ("", None),
        ("N/A", None),
        (None, None),
        (True, None),
        (float("nan"), None),
    ],
)
def test_parse_amount(text, expected):
    assert nz.parse_amount(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-14", "2026-10-14"),
        ("2026.10.14 08:42:13", "2026-10-14"),
        ("2026/10/14", "2026-10-14"),
        ("2026년 10월 14일", "2026-10-14"),
        ("20261014", "2026-10-14"),
        ("26.10.14", "2026-10-14"),
        ("10/13/2026 9:15 AM", "2026-10-13"),
        ("13/10/2026", "2026-10-13"),
        ("2026-02-30", None),
        ("yesterday", None),
        ("", None),
    ],
)
def test_normalize_date(text, expected):
    assert nz.normalize_date(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("08:42:13", "08:42"),
        ("8:42", "08:42"),
        ("8시 42분", "08:42"),
        ("오후 3:05", "15:05"),
        ("3:05 PM", "15:05"),
        ("9:15am", "09:15"),
        ("12:30 AM", "00:30"),
        ("오후 12:10", "12:10"),
        ("25:00", None),
        ("", None),
    ],
)
def test_normalize_time(text, expected):
    assert nz.normalize_time(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("KRW", "KRW"), ("krw", "KRW"), ("₩", "KRW"), ("원", "KRW"), ("won", "KRW"), ("$", "USD"), ("US$", "USD"),
     ("€", "EUR"), ("円", "JPY"), ("THB", "THB"), ("", "KRW"), ("money", "KRW")],
)
def test_normalize_currency(text, expected):
    assert nz.normalize_currency(text) == expected


def test_business_number_check_digit():
    assert nz.business_number_ok("1248100998")  # a well-known valid number
    assert nz.business_number_ok("1234567891")
    assert not nz.business_number_ok("1248100999")  # last digit misread
    assert not nz.business_number_ok("1248700998")  # a middle digit misread
    assert not nz.business_number_ok("124810099")
    assert not nz.business_number_ok("12481009981")
    assert nz.format_business_number("1248100998") == "124-81-00998"
    assert nz.format_business_number(" 124 81 00998 ") == "124-81-00998"
    assert nz.format_business_number("EIN 12-3456789") == "EIN 12-3456789"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("카드 9410-1234-5678-3456", "카드 ****-****-****-3456"),
        ("9410-12**-****-3456입니다", "****-****-****-3456입니다"),
        ("9410123456783456", "************3456"),
        ("XXXX XXXX XXXX 1234", "XXXX XXXX XXXX 1234"),
        ("3782-822463-10005", "****-******-10005"),
        ("TEL 02-123-4567", "TEL 02-123-4567"),
        ("010-1234-5678", "010-1234-5678"),
        ("123-45-67891", "123-45-67891"),
        ("2026-10-14 08:42:13", "2026-10-14 08:42:13"),
        ("GS25 선릉점", "GS25 선릉점"),
    ],
)
def test_mask_card_numbers(text, expected):
    assert nz.mask_card_numbers(text) == expected


def test_clean_text_collapses_whitespace_and_caps_length():
    assert nz.clean_text("  a\n b\t c ") == "a b c"
    assert len(nz.clean_text("가" * 500)) == nz.TEXT_LIMIT
    assert nz.clean_text(None) == ""


def test_amount_lines_keep_only_the_parts_of_the_total():
    lines = nz.amount_lines(
        [
            {"label": "Subtotal", "amount": "10.00"},
            {"label": "Tax", "amount": 0.86},
            {"label": "Tip", "amount": 2},
            {"label": "Total:", "amount": 12.86},
            {"label": "할인", "amount": 6000},
            {"label": "받은금액", "amount": 5000},
            {"label": "거스름돈", "amount": 800},
            {"label": "합계 금액", "amount": 4200},
            {"label": "no amount"},
            "부가세 818",
        ]
    )
    assert lines == [
        {"label": "Subtotal", "amount": 10.0},
        {"label": "Tax", "amount": 0.86},
        {"label": "Tip", "amount": 2.0},
        {"label": "할인", "amount": -6000.0},
    ]
    assert nz.amount_lines("공급가액 8,182") == []


@pytest.mark.parametrize(
    ("value", "category", "payment"),
    [
        ("meals", "meals", "unknown"),
        ("식대", "meals", "unknown"),
        ("Coffee shop", "cafe", "unknown"),
        ("택시", "transport", "unknown"),
        ("신용카드", "other", "card"),
        ("현금영수증", "other", "cash"),
        ("카카오페이", "other", "other"),
        ("", "other", "unknown"),
        (None, "other", "unknown"),
    ],
)
def test_category_and_payment_words(value, category, payment):
    assert nz.category(value) == category
    assert nz.payment(value) == payment


def test_field_names_keep_known_fields_once():
    assert nz.field_names(["total", "금액", "날짜", "Business number", "weather"]) == [
        "total",
        "date",
        "business_number",
    ]
    assert nz.field_names("total") == ["total"]
    assert nz.field_names(None) == []


def test_clean_text_drops_control_characters():
    assert nz.clean_text("달빛\x07커피\x00") == "달빛커피"
