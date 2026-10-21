"""Lazy mode: drop receipt photos into a folder all month, then turn the folder into an expense report.

    uv run python -m app.cli ~/Receipts/october
    uv run python -m app.cli ~/Receipts/october --out october.csv --lang en

Photos already read are remembered in .jeongsan-cache.json inside the folder (what was read, never the photo),
so running it again only reads the new ones.
"""

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

import httpx

from .checks import build_report, included, needs_review, to_receipt
from .config import MODEL, MODEL_DOWN, OLLAMA_URL, PHOTO_SUFFIXES
from .export import check_text, flag_text, to_csv, to_xlsx
from .images import BadPhoto, photo_id, prepare, sniff
from .model import ModelError, OllamaReader, Reader
from .normalize import ZERO_DECIMAL
from .prompts import PROMPT_VERSION
from .schemas import ReceiptRead, Report

CACHE_NAME = ".jeongsan-cache.json"


def money(amount: float | None, currency: str) -> str:
    if amount is None:
        return "?"
    return f"{amount:,.0f} {currency}" if currency in ZERO_DECIMAL else f"{amount:,.2f} {currency}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def load_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def read_folder(
    folder: Path, reader: Reader, today: date, *, cache: bool = True, say: Callable[[str], None] = print
) -> Report:
    photos = sorted(
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in PHOTO_SUFFIXES and not p.name.startswith(".")
    )
    cache_path = folder / CACHE_NAME
    known = load_cache(cache_path) if cache else {}
    tag = f"{reader.model}/{PROMPT_VERSION}"
    rows = []
    for n, path in enumerate(photos, 1):
        prefix = f"[{n}/{len(photos)}] {path.name}"
        data = path.read_bytes()
        pid = photo_id(data)
        read = None
        hit = known.get(pid)
        if isinstance(hit, dict) and hit.get("tag") == tag:
            try:
                read, seconds, note = ReceiptRead.model_validate(hit["read"]), float(hit["seconds"]), " (cached)"
            except (KeyError, TypeError, ValueError):  # a damaged cache entry: read the photo again
                read = None
        if read is None:
            if sniff(data) is None:
                say(f"{prefix}: skipped, not a JPEG, PNG, WebP or HEIC photo")
                continue
            try:
                image = prepare(data)
            except BadPhoto:
                say(f"{prefix}: skipped, could not open it")
                continue
            started = time.perf_counter()
            read = await reader.read(image)
            seconds, note = round(time.perf_counter() - started, 1), ""
            known[pid] = {"tag": tag, "read": read.model_dump(), "seconds": seconds}
            if cache:  # saved after every photo, so stopping halfway keeps what was read
                cache_path.write_text(json.dumps(known, ensure_ascii=False, indent=1), encoding="utf-8")
        row = to_receipt(read, pid, path.name, seconds)
        rows.append(row)
        what = (row.merchant or "?") if row.is_receipt else "not a receipt"
        say(f"{prefix}: {row.date or '?'}  {what}  {money(row.total, row.currency)}  {seconds:.1f}s{note}")
    return build_report(rows, today)


def describe(report: Report, out: Path) -> list[str]:
    s = report.summary
    totals = ", ".join(money(t.total, code) for code, t in s.totals.items()) or "nothing"
    lines = [
        "",
        f"{plural(s.count, 'receipt')}, {totals}" + (f", {plural(s.excluded, 'photo')} left out" if s.excluded else ""),
        f"Model time: {s.model_seconds:.0f}s in total",
    ]
    review = [row for row in report.rows if needs_review(row)]
    if review:
        lines.append(f"Needs a look ({len(review)}):")
        lines += [f"  - {row.file_name}: {check_text(row, 'en')}" for row in review]
    left_out = [row for row in report.rows if not included(row)]
    if left_out:
        lines.append("Left out:")
        lines += [f"  - {row.file_name}: {flag_text(row, row.flags[0], 'en')}" for row in left_out]
    lines.append(f"Saved {out}")
    return lines


async def run(args: argparse.Namespace, reader: Reader | None = None) -> int:
    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        print(f"{folder} is not a folder.", file=sys.stderr)
        return 2
    out = Path(args.out).expanduser() if args.out else folder / "jeongsan-report.xlsx"
    today = date.today()
    try:
        if reader is None:
            async with httpx.AsyncClient(base_url=args.ollama, timeout=300.0) as client:
                report = await read_folder(folder, OllamaReader(client, args.model), today, cache=not args.no_cache)
        else:
            report = await read_folder(folder, reader, today, cache=not args.no_cache)
    except ModelError as exc:
        print(f"\n{MODEL_DOWN}\n({exc})", file=sys.stderr)
        return 1
    content = to_csv(report, args.lang) if out.suffix.lower() == ".csv" else to_xlsx(report, args.lang, today)
    out.write_bytes(content)
    print("\n".join(describe(report, out)))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jeongsan", description="Turn a folder of receipt photos into a report.")
    parser.add_argument("folder", help="folder with receipt photos (JPEG, PNG, WebP or HEIC)")
    parser.add_argument("--out", help="report file, .xlsx or .csv (default: jeongsan-report.xlsx in the folder)")
    parser.add_argument("--lang", choices=("ko", "en"), default="ko", help="report language (default: ko)")
    parser.add_argument("--model", default=MODEL, help=f"Ollama model (default: {MODEL})")
    parser.add_argument("--ollama", default=OLLAMA_URL, help=f"Ollama URL (default: {OLLAMA_URL})")
    parser.add_argument("--no-cache", action="store_true", help="read every photo again")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
