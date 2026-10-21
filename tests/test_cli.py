import argparse
import csv
import io
import json

import pytest
from openpyxl import load_workbook

from app.cli import CACHE_NAME, parse_args, read_folder, run
from app.model import ModelError
from conftest import CAFE, TODAY, FakeReader

NOTE = {"is_receipt": False, "merchant": "", "business_number": "", "date": "", "time": "", "currency": "",
        "total": None, "breakdown": [], "summary": "", "category": "other", "payment": "unknown", "unreadable": []}


@pytest.fixture
def folder(tmp_path, make_photo):
    (tmp_path / "IMG_0001.jpg").write_bytes(make_photo(color="white"))
    (tmp_path / "IMG_0002.PNG").write_bytes(make_photo(color="black", fmt="PNG"))
    (tmp_path / "broken.jpg").write_bytes(b"not a photo")
    (tmp_path / "notes.txt").write_text("not a photo either")
    return tmp_path


@pytest.mark.anyio
async def test_read_folder_reads_each_photo_and_skips_the_rest(folder):
    said: list[str] = []
    reader = FakeReader(CAFE, NOTE)
    report = await read_folder(folder, reader, TODAY, say=said.append)

    assert len(reader.images) == 2
    assert [row.file_name for row in report.rows] == ["IMG_0001.jpg", "IMG_0002.PNG"]
    assert report.summary.count == 1 and report.summary.excluded == 1
    assert said[0] == "[1/3] IMG_0001.jpg: 2026-10-14  달빛커피 역삼점  9,000 KRW  0.0s"
    assert said[1].startswith("[2/3] IMG_0002.PNG: ?  not a receipt  ?")
    assert said[2] == "[3/3] broken.jpg: skipped, not a JPEG, PNG, WebP or HEIC photo"


@pytest.mark.anyio
async def test_second_run_reads_only_new_photos(folder, make_photo):
    await read_folder(folder, FakeReader(CAFE, NOTE), TODAY, say=lambda line: None)
    cache = json.loads((folder / CACHE_NAME).read_text(encoding="utf-8"))
    assert len(cache) == 2
    assert "images" not in json.dumps(cache)  # what was read, never the photo

    (folder / "IMG_0003.jpg").write_bytes(make_photo(color="red"))
    said: list[str] = []
    reader = FakeReader(CAFE)
    report = await read_folder(folder, reader, TODAY, say=said.append)

    assert len(reader.images) == 1  # only the new photo went to the model
    assert len(report.rows) == 3
    assert said[0].endswith("(cached)") and said[1].endswith("(cached)")


@pytest.mark.anyio
async def test_cache_is_ignored_for_another_model(folder):
    await read_folder(folder, FakeReader(CAFE, NOTE), TODAY, say=lambda line: None)
    other = FakeReader(CAFE, NOTE)
    other.model = "another-model"
    await read_folder(folder, other, TODAY, say=lambda line: None)

    assert len(other.images) == 2


@pytest.mark.anyio
async def test_no_cache_leaves_no_file(folder):
    await read_folder(folder, FakeReader(CAFE, NOTE), TODAY, cache=False, say=lambda line: None)
    assert not (folder / CACHE_NAME).exists()


def args(folder, *extra) -> argparse.Namespace:
    return parse_args([str(folder), *extra])


@pytest.mark.anyio
async def test_run_writes_an_excel_report_into_the_folder(folder, capsys):
    assert await run(args(folder), FakeReader(CAFE, NOTE)) == 0

    out = capsys.readouterr().out
    assert "1 receipt, 9,000 KRW, 1 photo left out" in out
    assert "IMG_0002.PNG: Not a receipt" in out
    wb = load_workbook(folder / "jeongsan-report.xlsx")
    assert wb["정산서"]["D6"].value == "달빛커피 역삼점"


@pytest.mark.anyio
async def test_run_writes_csv_in_english(folder, tmp_path):
    out = tmp_path / "october.csv"
    assert await run(args(folder, "--out", str(out), "--lang", "en"), FakeReader(CAFE, NOTE)) == 0

    rows = list(csv.reader(io.StringIO(out.read_text(encoding="utf-8-sig"))))
    assert rows[0][3] == "Merchant"
    assert rows[1][3] == "달빛커피 역삼점"


@pytest.mark.anyio
async def test_run_lists_rows_that_need_a_look(folder, capsys):
    await run(args(folder), FakeReader({**CAFE, "total": 6000}, NOTE))
    out = capsys.readouterr().out
    assert "IMG_0001.jpg: The receipt's own numbers don't add up: 8,182 + 818 = 9,000, total 6,000" in out


@pytest.mark.anyio
async def test_run_says_so_when_the_model_is_down(folder, capsys):
    assert await run(args(folder), FakeReader(ModelError("connection refused"))) == 1
    assert "Is Ollama running" in capsys.readouterr().err


@pytest.mark.anyio
async def test_run_needs_a_folder(tmp_path, capsys):
    assert await run(args(tmp_path / "missing"), FakeReader()) == 2


@pytest.mark.anyio
async def test_a_damaged_cache_entry_is_read_again(folder):
    await read_folder(folder, FakeReader(CAFE, NOTE), TODAY, say=lambda line: None)
    cache = json.loads((folder / CACHE_NAME).read_text(encoding="utf-8"))
    first = next(iter(cache))
    cache[first] = {"tag": cache[first]["tag"], "read": "garbage"}
    (folder / CACHE_NAME).write_text(json.dumps(cache), encoding="utf-8")

    reader = FakeReader(CAFE)
    await read_folder(folder, reader, TODAY, say=lambda line: None)
    assert len(reader.images) == 1
