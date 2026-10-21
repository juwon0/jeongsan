"""Runs the sample receipts through the real model and compares every field with samples/truth.json.

    uv run python scripts/bench.py                       # gemma4:e4b, or JEONGSAN_MODEL
    uv run python scripts/bench.py --model gemma4:12b --runs 3 --out docs/bench-gemma4-12b.json --readme README.md

The samples are drawn, not photographed, so treat the result as a smoke test of the whole pipeline on clean
receipts, not as accuracy on a real crumpled one. The end of the output is a Markdown table for the README.

Run 1 reads every photo for the first time. Later runs read the same photos again, which Ollama answers mostly from
its prompt cache, so they show whether the answers hold, not how long a new photo takes.
"""

import argparse
import asyncio
import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.checks import build_report, needs_review, to_receipt  # noqa: E402
from app.config import MODEL, OLLAMA_URL  # noqa: E402
from app.images import photo_id, prepare  # noqa: E402
from app.model import ModelError, OllamaReader  # noqa: E402
from app.schemas import Receipt  # noqa: E402

FIELDS = ("is_receipt", "merchant", "business_number", "date", "time", "currency", "total", "breakdown", "category",
          "payment")
TYPED = ("date", "time", "merchant", "business_number", "total")  # what a person would type into a spreadsheet
README_START, README_END = "<!-- bench:start -->", "<!-- bench:end -->"


def squash(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def not_printed(read: list[float], printed: list[float]) -> list[float]:
    """The amounts in a breakdown that the receipt doesn't print above its total (a discount's sign aside)."""
    left = [abs(amount) for amount in printed]
    missing = []
    for amount in read:
        match = next((p for p in left if abs(p - abs(amount)) < 0.005), None)
        if match is None:
            missing.append(amount)
        else:
            left.remove(match)
    return missing


def got(field: str, row: Receipt):
    value = getattr(row, field)
    return [line.amount for line in value] if field == "breakdown" else value


def right(field: str, want, row: Receipt) -> bool:
    value = got(field, row)
    if field == "merchant":
        return bool(want) and squash(want) in squash(value)
    if field == "business_number":
        return re.sub(r"\D", "", want) == re.sub(r"\D", "", value)
    if field == "total":
        return value is not None and abs(value - want) < 0.005
    if field == "breakdown":
        return not not_printed(value, want)
    return value == want


def machine() -> str:
    """Names the computer, so a number in the README says where it came from."""
    try:
        if sys.platform == "darwin":
            def sysctl(key: str) -> str:
                return subprocess.run(["sysctl", "-n", key], capture_output=True, text=True).stdout.strip()

            cpu, memory = sysctl("machdep.cpu.brand_string"), int(sysctl("hw.memsize"))
        else:
            info = Path("/proc/cpuinfo").read_text().splitlines()
            cpu = next((line.split(":", 1)[1].strip() for line in info if line.startswith("model name")), "")
            memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        return f"{cpu or platform.machine()}, {round(memory / 2**30)} GB"
    except (OSError, ValueError):
        return platform.machine()


async def load_model(client: httpx.AsyncClient, model: str) -> float | None:
    """Loads the model before the first photo, so the first photo's time is reading alone."""
    started = time.perf_counter()
    resp = await client.post("/api/generate", json={"model": model})
    if resp.status_code != 200:
        print(f"Could not load {model} ahead ({resp.status_code}); the first photo's time includes it", flush=True)
        return None
    return round(time.perf_counter() - started, 1)


async def read_all(names: list[str], reader: OllamaReader) -> list[Receipt]:
    rows = []
    for name in names:
        data = (ROOT / "samples" / name).read_bytes()
        started = time.perf_counter()
        read = await reader.read(prepare(data))
        seconds = round(time.perf_counter() - started, 1)
        rows.append(to_receipt(read, photo_id(data), name, seconds))
        print(f"  {name}: {seconds:.1f}s", flush=True)
    return rows


async def bench(model: str, ollama: str, runs: int) -> dict:
    truth = json.loads((ROOT / "samples" / "truth.json").read_text(encoding="utf-8"))
    main = [name for name in truth if "/" not in name]
    tricky = [name for name in truth if "/" in name]
    results = []
    async with httpx.AsyncClient(base_url=ollama, timeout=300.0) as client:
        try:
            version = (await client.get("/api/version")).json().get("version", "?")
        except (httpx.HTTPError, ValueError):
            version = "?"
        load_seconds = await load_model(client, model)
        if load_seconds is not None:
            print(f"Loaded {model} in {load_seconds:.1f}s", flush=True)
        reader = OllamaReader(client, model)
        for run in range(1, runs + 1):
            print(f"Run {run}/{runs} with {model}", flush=True)
            # The main set goes through one report, so duplicates are judged across photos as in the app.
            report = build_report(await read_all(main, reader), date.today())
            rows = report.rows + build_report(await read_all(tricky, reader), date.today()).rows
            for row in rows:
                want = truth[row.file_name]
                checked = [f for f in FIELDS if f in want] if want["is_receipt"] else ["is_receipt"]
                wrong = {f: {"want": want[f], "got": got(f, row)} for f in checked if not right(f, want[f], row)}
                if "breakdown" in wrong:
                    wrong["breakdown"] = {"not_on_the_receipt": not_printed(got("breakdown", row), want["breakdown"]),
                                          "got": got("breakdown", row)}
                results.append({
                    "run": run,
                    "file": row.file_name,
                    "seconds": row.seconds,
                    "fields": len(checked),
                    "wrong": wrong,
                    "total": row.total,
                    "flags": row.flags,
                    "needs_review": needs_review(row),
                    "expected_flags": want["expect_flags"],
                })
    return {"model": model, "ollama": version, "machine": machine(), "runs": runs, "date": date.today().isoformat(),
            "model_load_seconds": load_seconds, "results": results, "summary": summarize(results, truth)}


def spread(seconds: list[float]) -> dict | None:
    if not seconds:
        return None
    return {"mean": round(sum(seconds) / len(seconds), 1), "min": min(seconds), "max": max(seconds)}


def summarize(results: list[dict], truth: dict) -> dict:
    per_field: dict[str, list[int]] = {}
    for result in results:
        want = truth[result["file"]]
        for field in FIELDS if want["is_receipt"] else ("is_receipt",):
            if field in want:
                ok, total = per_field.setdefault(field, [0, 0])
                per_field[field] = [ok + (field not in result["wrong"]), total + 1]
    flagged = [r for r in results if r["needs_review"] or "not_receipt" in r["flags"]]
    expected = [r for r in results if r["expected_flags"]]
    # A wrong field that no flag pointed at is the failure that matters: it would slip into the report unseen.
    silent = [r for r in results if r["wrong"] and r not in flagged]
    typed = sum(
        len(str(want.get(f, "")))
        for name, want in truth.items()
        if "/" not in name and "possible_duplicate" not in want["expect_flags"]
        for f in TYPED
    )
    return {
        "field_accuracy": {f: f"{ok}/{total}" for f, (ok, total) in per_field.items()},
        "photos_all_right": sum(not r["wrong"] for r in results),
        "photos": len(results),
        "seconds_first_read": spread([r["seconds"] for r in results if r["run"] == 1]),
        "seconds_same_photo_again": spread([r["seconds"] for r in results if r["run"] > 1]),
        "flagged_for_review": sum(r["needs_review"] for r in results),
        "expected_flags_found": f"{sum(set(r['expected_flags']) <= set(r['flags']) for r in expected)}/{len(expected)}",
        "flagged_but_read_right": sum(not r["wrong"] and not r["expected_flags"] for r in flagged),
        "wrong_and_flagged": sum(bool(r["wrong"]) for r in flagged),
        "wrong_but_not_flagged": [{"run": r["run"], "file": r["file"], "wrong": r["wrong"]} for r in silent],
        "tricky_totals": {
            name: [r["total"] for r in results if r["file"] == name] for name in truth if "/" in name
        },
        "characters_you_would_type_for_the_main_set": typed,
    }


def markdown(result: dict) -> str:
    """The README table: what was measured, where, and how often a wrong field got past the checks."""
    s = result["summary"]
    counts = [tuple(map(int, value.split("/"))) for value in s["field_accuracy"].values()]
    ok, total = sum(c[0] for c in counts), sum(c[1] for c in counts)
    first, again = s["seconds_first_read"], s["seconds_same_photo_again"]
    lines = [
        f"| {result['model']} via Ollama {result['ollama']} | {result['machine']}, {result['date']} |",
        "|---|---|",
        f"| Photos read | {s['photos']} ({s['photos'] // result['runs']} samples × {result['runs']} run{'s' if result['runs'] > 1 else ''}) |",
        f"| Fields read right | {ok}/{total} ({ok / total:.0%}) |",
        f"| Photos with every field right | {s['photos_all_right']}/{s['photos']} |",
    ]
    if result["model_load_seconds"] is not None:
        lines.append(f"| Loading the model, once | {result['model_load_seconds']} s |")
    lines.append(f"| Seconds per photo, first read | {first['mean']} ({first['min']}–{first['max']}) |")
    if again:
        lines.append(f"| Same photo again (Ollama's prompt cache) | {again['mean']} ({again['min']}–{again['max']}) |")
    lines += [
        f"| Receipt photographed twice, and the sticky note, caught | {s['expected_flags_found']} |",
        f"| Photos with a wrong field that a check flagged | {s['wrong_and_flagged']} |",
        f"| Photos with a wrong field that no check flagged | {len(s['wrong_but_not_flagged'])} |",
        f"| Flagged although read right | {s['flagged_but_read_right']} |",
    ]
    for name, totals in s["tricky_totals"].items():
        lines.append(f"| `{name}` total as read | {', '.join(f'{t:,.0f}' if t is not None else '–' for t in totals)} |")
    lines += ["", "Per field: " + ", ".join(f"{field} {value}" for field, value in s["field_accuracy"].items()) + "."]
    return "\n".join(lines)


def into_readme(path: Path, table: str) -> None:
    """Puts the table between the two marker comments, so the README always shows the last measured run."""
    text = path.read_text(encoding="utf-8")
    head, start, rest = text.partition(README_START)
    _, end, tail = rest.partition(README_END)
    if not (start and end):
        raise SystemExit(f"{path} has no {README_START} ... {README_END} block")
    path.write_text(f"{head}{README_START}\n{table}\n{README_END}{tail}", encoding="utf-8")


def describe(field: str, wrong: dict) -> str:
    if field == "breakdown":
        return f"breakdown: {wrong['not_on_the_receipt']} not on the receipt (read {wrong['got']})"
    return f"{field}: want {wrong['want']!r} got {wrong['got']!r}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--ollama", default=OLLAMA_URL)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--out", help="save the full result as JSON")
    parser.add_argument("--readme", help=f"write the table into this file, between {README_START} and {README_END}")
    args = parser.parse_args()
    try:
        result = asyncio.run(bench(args.model, args.ollama, args.runs))
    except (ModelError, httpx.HTTPError) as exc:
        print(f"The model did not answer: {exc}", file=sys.stderr)
        return 1
    print()
    for r in result["results"]:
        mark = "ok " if not r["wrong"] else "BAD"
        wrong = "; ".join(describe(f, v) for f, v in r["wrong"].items())
        print(f"{mark} {r['file']:<22} {r['seconds']:>5.1f}s  flags={','.join(r['flags']) or '-'}  {wrong}")
    print()
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print()
    table = markdown(result)
    print(table)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\nSaved {out}")
    if args.readme:
        into_readme(Path(args.readme), table)
        print(f"Wrote the table into {args.readme}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
