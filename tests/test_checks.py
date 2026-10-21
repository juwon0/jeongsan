from app.checks import build_report, needs_review, to_receipt
from app.schemas import ReceiptRead
from conftest import CAFE, TODAY


def flags(row):
    return build_report([row], TODAY).rows[0].flags


def test_a_clean_card_slip_has_no_flags(make_row):
    report = build_report([make_row()], TODAY)

    assert report.rows[0].flags == []
    assert report.summary.count == 1
    assert report.summary.needs_review == 0
    assert report.summary.totals["KRW"].total == 9000
    assert report.summary.totals["KRW"].by_category == {"cafe": 9000}
    assert report.summary.period == ("2026-10-14", "2026-10-14")


def test_fields_the_model_could_not_read_are_flagged(make_row):
    assert flags(make_row(unreadable=["total"])) == ["unreadable"]


def test_missing_total_is_flagged(make_row):
    assert flags(make_row(total=None, breakdown=[])) == ["no_total"]
    assert flags(make_row(total=0, breakdown=[])) == ["no_total"]


def test_dates_that_cannot_be_right_are_flagged(make_row):
    assert flags(make_row(date="2026-10-21")) == []  # tomorrow: a time zone away
    assert flags(make_row(date="2026-10-23")) == ["bad_date"]
    assert flags(make_row(date="2016-10-14")) == ["bad_date"]  # 2026 misread as 2016
    assert flags(make_row(date="")) == ["bad_date"]
    assert flags(make_row(date="어제")) == ["bad_date"]


def test_breakdown_that_does_not_add_up_to_the_total_is_flagged(make_row):
    assert flags(make_row(total=6000)) == ["sum_mismatch"]  # 8,182 + 818 is 9,000
    assert flags(make_row(breakdown=[])) == []  # nothing to check against


def test_breakdown_with_cents_tip_and_discount(make_row):
    usd = dict(currency="USD", business_number="", date="10/13/2026")
    lines = [{"label": "Subtotal", "amount": 10.0}, {"label": "Tax", "amount": 0.86}, {"label": "Tip", "amount": 2}]
    assert flags(make_row(**usd, total=12.86, breakdown=lines)) == []
    assert flags(make_row(**usd, total=12.68, breakdown=lines)) == ["sum_mismatch"]
    parking = [{"label": "주차요금", "amount": 12000}, {"label": "할인", "amount": 6000}]
    assert flags(make_row(total=6000, breakdown=parking)) == []


def test_business_number_with_a_wrong_check_digit_is_flagged(make_row):
    assert flags(make_row(business_number="123-45-67892")) == ["bad_business_number"]
    assert flags(make_row(business_number="123-45-6789")) == ["bad_business_number"]
    assert flags(make_row(business_number="EIN 12-3456789", currency="USD")) == []  # only Korean numbers carry one


def test_blank_business_number_on_a_korean_receipt_is_flagged(make_row):
    assert flags(make_row(business_number="")) == ["no_business_number"]
    assert flags(make_row(business_number="", unreadable=["business_number"])) == ["unreadable"]  # said so already
    assert flags(make_row(business_number="", currency="USD")) == []  # a foreign receipt has none to print


def test_the_same_photo_twice_is_left_out(make_row):
    report = build_report([make_row(), make_row()], TODAY)

    assert [row.flags for row in report.rows] == [[], ["duplicate_photo"]]
    assert report.summary.count == 1
    assert report.summary.excluded == 1
    assert report.summary.totals["KRW"].total == 9000


def test_two_photos_of_one_receipt_are_flagged_but_kept(make_row):
    again = make_row(id="1" * 16, merchant="달빛 커피")  # read a little differently the second time
    report = build_report([make_row(), again], TODAY)

    assert [row.flags for row in report.rows] == [[], ["possible_duplicate"]]
    assert report.summary.count == 2
    assert report.summary.needs_review == 1


def test_same_amount_at_another_time_is_not_a_duplicate(make_row):
    report = build_report([make_row(), make_row(id="1" * 16, time="15:10")], TODAY)

    assert [row.flags for row in report.rows] == [[], []]


def test_without_a_time_the_store_name_decides(make_row):
    first = make_row(time="")
    same_store = make_row(id="1" * 16, time="", merchant="(주)달빛커피 역삼점")
    other_store = make_row(id="2" * 16, time="", merchant="한빛문구")
    report = build_report([first, same_store, other_store], TODAY)

    assert [row.flags for row in report.rows] == [[], ["possible_duplicate"], []]


def test_a_photo_that_is_not_a_receipt_is_left_out(make_row):
    note = make_row(id="1" * 16, is_receipt=False, total=None, merchant="", date="")
    report = build_report([make_row(), note], TODAY)

    assert report.rows[1].flags == ["not_receipt"]
    assert report.summary.count == 1
    assert report.summary.excluded == 1
    assert report.summary.needs_review == 0


def test_a_checked_row_keeps_its_flags_but_needs_no_more_review(make_row):
    report = build_report([make_row(total=6000, reviewed=True)], TODAY)

    assert report.rows[0].flags == ["sum_mismatch"]
    assert not needs_review(report.rows[0])
    assert report.summary.needs_review == 0


def test_flags_from_the_browser_are_recomputed(make_row):
    assert flags(make_row(flags=["not_receipt", "duplicate_photo"])) == []


def test_currencies_are_totalled_separately_won_first(make_row):
    rows = [
        make_row(id="1" * 16, currency="USD", business_number="", total=12.86, breakdown=[], date="2026-10-13"),
        make_row(),
        make_row(id="2" * 16, time="12:31", total=8500, breakdown=[], category="meals"),
    ]
    summary = build_report(rows, TODAY).summary

    assert list(summary.totals) == ["KRW", "USD"]
    assert summary.totals["KRW"].total == 17500
    assert summary.totals["KRW"].by_category == {"meals": 8500, "cafe": 9000}
    assert summary.totals["USD"].total == 12.86
    assert summary.period == ("2026-10-13", "2026-10-14")


def test_typed_values_are_tidied_and_unparseable_ones_kept(make_row):
    report = build_report([make_row(date="2026.10.14", time="오전 8:42", currency="₩", total=9000.4)], TODAY)
    row = report.rows[0]

    assert (row.date, row.time, row.currency, row.total) == ("2026-10-14", "08:42", "KRW", 9000)
    assert build_report([make_row(date="지난주")], TODAY).rows[0].date == "지난주"


def test_model_seconds_add_up_over_every_photo(make_row):
    rows = [make_row(seconds=8.2), make_row(seconds=7.9), make_row(id="1" * 16, is_receipt=False, seconds=3.0)]
    assert build_report(rows, TODAY).summary.model_seconds == 19.1


def test_to_receipt_masks_a_card_number_the_model_copied():
    read = ReceiptRead.model_validate({**CAFE, "summary": "카드 9410-1234-5678-3456 일시불"})
    row = to_receipt(read, "a" * 16, "IMG_0001.HEIC", 8.24)

    assert row.summary == "카드 ****-****-****-3456 일시불"
    assert row.file_name == "IMG_0001.HEIC"
    assert row.seconds == 8.24
