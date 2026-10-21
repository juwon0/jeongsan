import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from PIL import Image

from app import main
from app.main import app, get_reader, get_today
from app.model import ModelError
from conftest import CAFE, TODAY, FakeReader


@pytest.fixture
def client():
    reader = FakeReader()
    app.dependency_overrides[get_reader] = lambda: reader
    app.dependency_overrides[get_today] = lambda: TODAY
    with TestClient(app) as test_client:
        test_client.reader = reader
        yield test_client
    app.dependency_overrides.clear()


def upload(client, data: bytes, name: str = "IMG_0001.jpg"):
    return client.post("/api/receipts", files={"photo": (name, data, "image/jpeg")})


def test_index_and_health(client):
    assert "Jeongsan" in client.get("/").text
    health = client.get("/healthz").json()
    assert health["ok"] is True and health["model"] == main.MODEL


def test_labels_come_in_both_languages(client):
    labels = client.get("/api/labels").json()
    assert labels["ko"]["categories"]["meals"] == "식대"
    assert labels["en"]["flags"]["sum_mismatch"].startswith("The receipt's own numbers don't add up: {lines}")


def test_samples_list_only_photos(client):
    photos = client.get("/api/samples").json()["photos"]
    assert all(name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".heic")) for name in photos)


def test_a_photo_becomes_a_checked_row(client, make_photo):
    resp = upload(client, make_photo((3000, 4000)))

    assert resp.status_code == 200
    row = resp.json()
    assert row["merchant"] == "달빛커피 역삼점"
    assert row["file_name"] == "IMG_0001.jpg"
    assert len(row["id"]) == 16
    assert row["flags"] == []
    assert row["seconds"] >= 0
    shown = Image.open(io.BytesIO(client.reader.images[0]))  # the model got the shrunk JPEG
    assert shown.format == "JPEG" and max(shown.size) == 1800


def test_flags_come_back_with_the_row(client, make_photo):
    client.reader.answers = [{**CAFE, "total": 6000, "unreadable": ["total"]}]
    assert upload(client, make_photo()).json()["flags"] == ["unreadable", "sum_mismatch"]


def test_too_big_photo_is_refused(client, make_photo, monkeypatch):
    monkeypatch.setattr(main, "MAX_PHOTO_BYTES", 100)
    assert upload(client, make_photo((300, 300))).status_code == 413


@pytest.mark.parametrize("data", [b"%PDF-1.7 not a photo", b"\xff\xd8\xff but not a jpeg"])
def test_files_that_are_not_photos_are_refused(client, data):
    assert upload(client, data, "receipt.jpg").status_code == 415


def test_model_down_is_a_clear_502(client, make_photo):
    client.reader.answers = [ModelError("connection refused")]
    resp = upload(client, make_photo())

    assert resp.status_code == 502
    assert "Ollama" in resp.json()["detail"]


def test_report_recomputes_flags_over_all_rows(client, make_row):
    first = make_row().model_dump()
    again = make_row(id="1" * 16, flags=[]).model_dump()
    same_photo = make_row(flags=[]).model_dump()
    resp = client.post("/api/report", json={"rows": [first, again, same_photo]})

    body = resp.json()
    assert [row["flags"] for row in body["rows"]] == [[], ["possible_duplicate"], ["duplicate_photo"]]
    assert body["summary"]["count"] == 2
    assert body["summary"]["needs_review"] == 1


def test_report_rejects_a_bad_row(client, make_row):
    bad = {**make_row().model_dump(), "id": "../../etc/passwd"}
    assert client.post("/api/report", json={"rows": [bad]}).status_code == 422


def test_excel_download(client, make_row):
    resp = client.post("/api/report.xlsx?lang=en", json={"rows": [make_row().model_dump()]})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert 'filename="jeongsan-2026-10-20.xlsx"' in resp.headers["content-disposition"]
    assert load_workbook(io.BytesIO(resp.content))["Report"]["A1"].value == "Expense report"


def test_csv_download(client, make_row):
    resp = client.post("/api/report.csv", json={"rows": [make_row().model_dump()]})

    assert resp.status_code == 200
    assert resp.content.startswith("﻿번호".encode())
    assert 'filename="jeongsan-2026-10-20.csv"' in resp.headers["content-disposition"]
