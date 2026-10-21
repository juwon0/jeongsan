"""Turns what a small model writes into clean values, and checks the ones that carry their own proof."""

import math
import re
from datetime import date
from typing import Any

TEXT_LIMIT = 80
MAX_LINES = 8
ZERO_DECIMAL = {"KRW", "JPY", "VND"}

CATEGORY_WORDS = {
    "meals": ("meal", "food", "restaurant", "dining", "lunch", "dinner", "식대", "식사", "음식", "식당"),
    "cafe": ("cafe", "café", "coffee", "drink", "beverage", "bakery", "카페", "커피", "음료", "베이커리"),
    "transport": ("transport", "taxi", "train", "bus", "fuel", "parking", "toll", "교통", "택시", "주차", "주유"),
    "lodging": ("lodging", "hotel", "accommodation", "숙박", "호텔"),
    "supplies": ("suppl", "office", "stationery", "소모품", "문구", "사무"),
}
PAYMENT_WORDS = {
    "card": ("card", "credit", "debit", "visa", "master", "카드", "신용", "체크"),
    "cash": ("cash", "현금"),
    "other": ("transfer", "gift", "point", "voucher", "mobile", "계좌", "이체", "상품권", "포인트", "페이", "간편"),
}
FIELD_WORDS = {
    "merchant": ("merchant", "store", "vendor", "shop", "name", "상호", "가맹점", "사용처"),
    "business_number": ("business", "registration", "사업자"),
    "date": ("date", "날짜", "일자"),
    "time": ("time", "시간", "시각"),
    "total": ("total", "amount", "합계", "금액"),
    "breakdown": ("breakdown", "subtotal", "tax", "vat", "공급가", "부가세"),
}
# Lines a model sometimes copies into the breakdown although they are the total itself or the cash handed over.
NOT_A_PART = re.compile(
    r"^(?:합\s*계|총\s*합\s*계|총\s*계|총\s*액|총\s*금\s*액|결\s*제|받\s*을|승\s*인|청\s*구)\s*(?:금\s*액)?$"
    r"|^(?:grand\s*)?total(?:\s*(?:amount|due|paid))?$|^amount\s*(?:due|paid)$"
    r"|^(?:받은\s*금액|받은\s*돈|거스름\s*돈?|잔\s*돈|change|cash\s*tendered|tendered)$",
    re.IGNORECASE,
)
DISCOUNT = re.compile(r"할인|쿠폰|discount|coupon", re.IGNORECASE)
CURRENCY_ALIASES = {"WON": "KRW", "RMB": "CNY", "YEN": "JPY", "DOLLAR": "USD", "DOLLARS": "USD"}
CURRENCY_SYMBOLS = (("US$", "USD"), ("₩", "KRW"), ("원", "KRW"), ("$", "USD"), ("€", "EUR"), ("£", "GBP"),
                    ("円", "JPY"), ("¥", "JPY"), ("元", "CNY"), ("₫", "VND"))

# 13 to 19 digits, possibly masked with * or X and grouped by spaces or dashes: a card number.
_CARD_LIKE = re.compile(r"(?<![\d*])[\dXx*]{2,6}(?:[ -][\dXx*]{2,6}){1,5}(?![\d*])|(?<![\d*])[\d*]{13,19}(?![\d*])")


def mask_card_numbers(text: str) -> str:
    """Star out every digit of a card number except the last group, the way receipts print them."""

    def mask(match: re.Match) -> str:
        chunk = match.group(0)
        if sum(c.isdigit() or c in "*xX" for c in chunk) < 13:
            return chunk  # dates, phone numbers and business numbers are shorter
        cut = max(chunk.rfind(" "), chunk.rfind("-"))
        if cut == -1:
            cut = len(chunk) - 4
        return re.sub(r"\d", "*", chunk[:cut]) + chunk[cut:]

    return _CARD_LIKE.sub(mask, text)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = re.sub(r"[\x00-\x1f\x7f]", "", " ".join(str(value).split()))  # control characters break .xlsx files
    return mask_card_numbers(text)[:TEXT_LIMIT]


def parse_amount(value: Any) -> float | None:
    """Reads 9000, "9,000원", "₩9,000", "$12.86" or "(1,000)". Anything else is None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    text = str(value).strip()
    negative = text.startswith(("-", "−")) or (text.startswith("(") and text.endswith(")"))
    digits = re.sub(r"[^\d.]", "", text).strip(".")
    if digits.count(".") > 1:  # 1.234.567 groups thousands with dots
        digits = digits.replace(".", "")
    try:
        number = float(digits)
    except ValueError:
        return None
    return -number if negative else number


def round_amount(value: float, currency: str) -> float:
    return float(round(value, 0 if currency in ZERO_DECIMAL else 2))


def _pick(value: Any, words: dict[str, tuple[str, ...]], fallback: str) -> str:
    text = str(value or "").strip().lower()
    if text in words or text == fallback:
        return text
    for key, hints in words.items():
        if any(hint in text for hint in hints):
            return key
    return fallback


def category(value: Any) -> str:
    return _pick(value, CATEGORY_WORDS, "other")


def payment(value: Any) -> str:
    return _pick(value, PAYMENT_WORDS, "unknown")


def field_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        value = [value] if value else []
    names: list[str] = []
    for item in value:
        name = _pick(item, FIELD_WORDS, "")
        if name and name not in names:
            names.append(name)
    return names


def amount_lines(value: Any) -> list[dict]:
    """Keeps the breakdown lines that have an amount; drops the ones that are really the total or the change."""
    lines: list[dict] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        label = clean_text(item.get("label"))
        amount = parse_amount(item.get("amount"))
        if amount is None or NOT_A_PART.match(label.rstrip(": ")):
            continue
        if DISCOUNT.search(label) and amount > 0:
            amount = -amount  # the minus sign is easy to drop when reading a discount line
        lines.append({"label": label, "amount": amount})
    return lines[:MAX_LINES]


def normalize_currency(text: str, default: str = "KRW") -> str:
    code = text.strip().upper()
    if code in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[code]
    if re.fullmatch(r"[A-Z]{3}", code):
        return code
    for symbol, iso in CURRENCY_SYMBOLS:
        if symbol in code:
            return iso
    return default


def normalize_date(text: str) -> str | None:
    """2026-10-14, 2026.10.14, 2026년 10월 14일, 20261014, 26.10.14 and 10/14/2026 all become 2026-10-14."""
    patterns = (
        (r"(?<!\d)(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})(?!\d)", "ymd"),
        (r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)", "ymd"),
        (r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)", "mdy"),  # US receipts
        (r"(?<!\d)(\d{2})[-./](\d{1,2})[-./](\d{1,2})(?!\d)", "yy"),  # Korean receipts shorten the year
    )
    for pattern, order in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        a, b, c = (int(group) for group in match.groups())
        if order == "ymd":
            y, m, d = a, b, c
        elif order == "mdy":
            y, m, d = (c, a, b) if a <= 12 else (c, b, a)
        else:
            y, m, d = 2000 + a, b, c
        try:
            return date(y, m, d).isoformat()
        except ValueError:
            return None
    return None


def normalize_time(text: str) -> str | None:
    """08:42:13, 8시 42분, 오후 3:05 and 3:05 PM become 24-hour HH:MM."""
    match = re.search(r"(\d{1,2})\s*[:시]\s*(\d{2})", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if re.search(r"오후|(?<![a-z])p\.?m(?![a-z])", text, re.IGNORECASE) and hour < 12:
        hour += 12
    elif re.search(r"오전|(?<![a-z])a\.?m(?![a-z])", text, re.IGNORECASE) and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def business_number_ok(digits: str) -> bool:
    """Korean business registration numbers end in a check digit, so one misread digit shows up as arithmetic."""
    if len(digits) != 10 or not digits.isdigit():
        return False
    weights = (1, 3, 7, 1, 3, 7, 1, 3, 5)
    total = sum(int(d) * w for d, w in zip(digits, weights)) + int(digits[8]) * 5 // 10
    return (10 - total % 10) % 10 == int(digits[9])


def format_business_number(text: str) -> str:
    digits = re.sub(r"\D", "", text)
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    return text.strip()
