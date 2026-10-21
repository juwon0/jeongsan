from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from . import normalize as nz

Category = Literal["meals", "cafe", "transport", "lodging", "supplies", "other"]
Payment = Literal["card", "cash", "other", "unknown"]
FieldName = Literal["merchant", "business_number", "date", "time", "total", "breakdown"]
Flag = Literal[
    "not_receipt",  # excluded from the totals
    "duplicate_photo",  # excluded: the very same photo came twice
    "unreadable",
    "no_total",
    "bad_date",
    "sum_mismatch",
    "bad_business_number",
    "no_business_number",
    "possible_duplicate",
]

# Small models answer in many shapes ("9,000원", "식대", null), so every field is lenient on the way in.
Text = Annotated[str, BeforeValidator(nz.clean_text)]
Money = Annotated[float | None, BeforeValidator(nz.parse_amount)]
CategoryField = Annotated[Category, BeforeValidator(nz.category)]
PaymentField = Annotated[Payment, BeforeValidator(nz.payment)]
Unreadable = Annotated[list[FieldName], BeforeValidator(nz.field_names)]
IsReceipt = Annotated[bool, BeforeValidator(lambda value: True if value is None else value)]


class Amount(BaseModel):
    label: Text
    amount: float


Lines = Annotated[list[Amount], BeforeValidator(nz.amount_lines)]


class ReceiptFields(BaseModel):
    is_receipt: IsReceipt = True
    merchant: Text = ""
    business_number: Text = ""
    date: Text = ""
    time: Text = ""
    currency: Text = ""
    total: Money = None
    breakdown: Lines = []
    summary: Text = ""
    category: CategoryField = "other"
    payment: PaymentField = "unknown"
    unreadable: Unreadable = []


class ReceiptRead(ReceiptFields):
    """One receipt photo, read for an expense report."""  # the model sees this in the JSON Schema

    # Validation keeps the defaults, but the schema the model fills in lists every field as required.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class Receipt(ReceiptFields):
    """One row of the expense report, as the browser shows and edits it."""

    id: str = Field(pattern=r"^[0-9a-f]{16}$")  # start of the photo's SHA-256
    file_name: Text = ""
    seconds: float = Field(0, ge=0)  # time the model spent on this photo
    reviewed: bool = False  # a person looked at the flags and is fine with the row
    flags: list[Flag] = []  # recomputed by the server on every report, never trusted from the browser


class ReportRequest(BaseModel):
    rows: list[Receipt] = Field(default_factory=list, max_length=500)


class CurrencyTotal(BaseModel):
    total: float
    by_category: dict[Category, float]


class Summary(BaseModel):
    count: int  # rows in the report
    excluded: int  # photos left out: not a receipt, or the same photo twice
    needs_review: int
    totals: dict[str, CurrencyTotal]  # one entry per currency, never added across currencies
    period: tuple[str, str] | None
    model_seconds: float


class Report(BaseModel):
    rows: list[Receipt]
    summary: Summary
