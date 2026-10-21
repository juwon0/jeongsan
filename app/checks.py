"""The part that doesn't trust the model: every row is checked with plain arithmetic before it reaches the report."""

import re
from datetime import date, timedelta

from . import normalize as nz
from .config import DEFAULT_CURRENCY
from .schemas import Category, CurrencyTotal, Flag, Receipt, ReceiptRead, Report, Summary

EXCLUDING: frozenset[Flag] = frozenset({"not_receipt", "duplicate_photo"})
NEEDS_A_LOOK: frozenset[Flag] = frozenset(
    {
        "unreadable",
        "no_total",
        "bad_date",
        "sum_mismatch",
        "bad_business_number",
        "no_business_number",
        "possible_duplicate",
    }
)
CATEGORIES: tuple[Category, ...] = ("meals", "cafe", "transport", "lodging", "supplies", "other")


def to_receipt(read: ReceiptRead, photo: str, file_name: str, seconds: float) -> Receipt:
    return normalized(Receipt(id=photo, file_name=file_name, seconds=seconds, **read.model_dump()))


def normalized(row: Receipt) -> Receipt:
    """Cleans what the model or a person typed. Values that can't be parsed stay as typed, and get flagged."""
    currency = nz.normalize_currency(row.currency, DEFAULT_CURRENCY)
    return row.model_copy(
        update={
            "currency": currency,
            "date": nz.normalize_date(row.date) or row.date,
            "time": nz.normalize_time(row.time) or row.time,
            "business_number": nz.format_business_number(row.business_number),
            "total": None if row.total is None else nz.round_amount(row.total, currency),
            "breakdown": [
                line.model_copy(update={"amount": nz.round_amount(line.amount, currency)}) for line in row.breakdown
            ],
        }
    )


def date_ok(text: str, today: date) -> bool:
    iso = nz.normalize_date(text)
    if iso is None:
        return False
    # A year in the future or long past is far more likely a misread digit than a real receipt.
    return today - timedelta(days=366) <= date.fromisoformat(iso) <= today + timedelta(days=1)


def row_flags(row: Receipt, today: date) -> list[Flag]:
    if not row.is_receipt:
        return ["not_receipt"]
    flags: list[Flag] = []
    if row.unreadable:
        flags.append("unreadable")
    if row.total is None or row.total <= 0:
        flags.append("no_total")
    if not date_ok(row.date, today):
        flags.append("bad_date")
    if row.total is not None and row.breakdown:
        # The receipt's own arithmetic: 공급가액 + 부가세 = 합계, or subtotal + tax + tip = total.
        tolerance = 0.5 if row.currency in nz.ZERO_DECIMAL else 0.005
        if abs(sum(line.amount for line in row.breakdown) - row.total) > tolerance:
            flags.append("sum_mismatch")
    if row.currency == "KRW":
        digits = re.sub(r"\D", "", row.business_number)
        if digits and not nz.business_number_ok(digits):
            flags.append("bad_business_number")
        elif not digits and "business_number" not in row.unreadable:
            # Korean receipts print the store's number and an expense report needs it, so a blank one is a miss.
            flags.append("no_business_number")
    return flags


def _same_purchase(row: Receipt) -> tuple | None:
    """Two photos of one receipt: same date and amount, and the same minute or (without a time) the same store."""
    if row.total is None or nz.normalize_date(row.date) is None:
        return None
    if nz.normalize_time(row.time):
        return (row.date, row.time, row.currency, row.total)
    store = re.sub(r"[\W_]+", "", re.sub(r"\(주\)|주식회사", "", row.merchant.lower()))
    return (row.date, row.currency, row.total, store) if store else None


def build_report(rows: list[Receipt], today: date) -> Report:
    """Recomputes every flag and the totals. Rows keep their order, so the browser can match them up."""
    photos: set[str] = set()
    purchases: set[tuple] = set()
    checked: list[Receipt] = []
    for row in rows:
        row = normalized(row)
        if row.id in photos:
            flags: list[Flag] = ["duplicate_photo"]
        else:
            flags = row_flags(row, today)
            key = _same_purchase(row) if row.is_receipt else None
            if key is not None:
                if key in purchases:
                    flags.append("possible_duplicate")
                purchases.add(key)
        photos.add(row.id)
        checked.append(row.model_copy(update={"flags": flags}))
    return Report(rows=checked, summary=summarize(checked))


def included(row: Receipt) -> bool:
    return not EXCLUDING.intersection(row.flags)


def needs_review(row: Receipt) -> bool:
    return included(row) and not row.reviewed and bool(NEEDS_A_LOOK.intersection(row.flags))


def summarize(rows: list[Receipt]) -> Summary:
    kept = [row for row in rows if included(row)]
    sums: dict[str, dict[Category, float]] = {}
    for row in kept:
        if row.total is not None:
            by_category = sums.setdefault(row.currency, {})
            by_category[row.category] = by_category.get(row.category, 0.0) + row.total
    totals = {
        currency: CurrencyTotal(
            total=nz.round_amount(sum(by_category.values()), currency),
            by_category={c: nz.round_amount(by_category[c], currency) for c in CATEGORIES if c in by_category},
        )
        # Won first, then the rest in alphabetical order; currencies are never added together.
        for currency, by_category in sorted(sums.items(), key=lambda item: (item[0] != DEFAULT_CURRENCY, item[0]))
    }
    dates = sorted(iso for row in kept if (iso := nz.normalize_date(row.date)))
    return Summary(
        count=len(kept),
        excluded=len(rows) - len(kept),
        needs_review=sum(needs_review(row) for row in rows),
        totals=totals,
        period=(dates[0], dates[-1]) if dates else None,
        model_seconds=round(sum(row.seconds for row in rows), 1),
    )
