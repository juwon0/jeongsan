import logging
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .checks import build_report, to_receipt
from .config import MODEL, MODEL_DOWN, OLLAMA_URL, PHOTO_SUFFIXES
from .export import LABELS, MIME_XLSX, Lang, to_csv, to_xlsx
from .images import BadPhoto, photo_id, prepare, sniff
from .model import ModelError, OllamaReader, Reader
from .schemas import Receipt, Report, ReportRequest

MAX_PHOTO_BYTES = 20 * 1024 * 1024
STATIC_DIR = Path(__file__).parent / "static"
SAMPLES_DIR = Path(__file__).parent.parent / "samples"

log = logging.getLogger("uvicorn.error")  # prints next to uvicorn's own lines


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A big photo on a laptop without a GPU can take a while, so the timeout is generous.
    async with httpx.AsyncClient(base_url=OLLAMA_URL, timeout=300.0) as client:
        app.state.reader = OllamaReader(client, MODEL)
        yield


app = FastAPI(title="Jeongsan", lifespan=lifespan)
app.mount("/samples", StaticFiles(directory=SAMPLES_DIR, check_dir=False), name="samples")


def get_reader(request: Request) -> Reader:
    return request.app.state.reader


def get_today() -> date:
    return date.today()


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "model": MODEL, "ollama": OLLAMA_URL}


@app.get("/api/labels")
async def labels() -> dict:
    keys = ("categories", "payments", "fields", "flags", "reviewed")
    return {lang: {key: LABELS[lang][key] for key in keys} for lang in LABELS}


@app.get("/api/samples")
async def samples() -> dict:
    if not SAMPLES_DIR.is_dir():
        return {"photos": []}
    return {"photos": sorted(p.name for p in SAMPLES_DIR.iterdir() if p.suffix.lower() in PHOTO_SUFFIXES)}


@app.post("/api/receipts", response_model=Receipt)
async def read_receipt(
    photo: UploadFile = File(...), reader: Reader = Depends(get_reader), today: date = Depends(get_today)
) -> Receipt:
    # Read one byte past the limit so an oversized upload is caught without buffering all of it.
    data = await photo.read(MAX_PHOTO_BYTES + 1)
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="Photo is larger than 20 MB.")
    if sniff(data) is None:
        raise HTTPException(status_code=415, detail="Upload a JPEG, PNG, WebP or HEIC photo.")
    # The photo lives in memory for this request only and is never written to disk.
    try:
        image = await run_in_threadpool(prepare, data)
    except BadPhoto as exc:
        raise HTTPException(status_code=415, detail="This photo could not be opened.") from exc
    started = time.perf_counter()
    try:
        read = await reader.read(image)
    except ModelError as exc:
        log.warning("reading %s failed: %s", photo.filename, exc)
        raise HTTPException(status_code=502, detail=MODEL_DOWN) from exc
    row = to_receipt(read, photo_id(data), photo.filename or "", round(time.perf_counter() - started, 1))
    return build_report([row], today).rows[0]


@app.post("/api/report", response_model=Report)
async def report(req: ReportRequest, today: date = Depends(get_today)) -> Report:
    return build_report(req.rows, today)


@app.post("/api/report.xlsx")
async def report_xlsx(req: ReportRequest, lang: Lang = "ko", today: date = Depends(get_today)) -> Response:
    content = to_xlsx(build_report(req.rows, today), lang, today)
    return download(content, MIME_XLSX, f"jeongsan-{today}.xlsx")


@app.post("/api/report.csv")
async def report_csv(req: ReportRequest, lang: Lang = "ko", today: date = Depends(get_today)) -> Response:
    content = to_csv(build_report(req.rows, today), lang)
    return download(content, "text/csv; charset=utf-8", f"jeongsan-{today}.csv")


def download(content: bytes, media_type: str, name: str) -> Response:
    return Response(content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{name}"'})
