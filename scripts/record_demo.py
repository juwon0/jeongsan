"""Records docs/demo.gif: the real app reading the sample receipts with the real model, the waits cut short.

    uv run uvicorn app.main:app --port 8000                  # Ollama running, model pulled
    uv run --with playwright python scripts/record_demo.py   # in a second terminal

It drives the installed Google Chrome (or Playwright's own Chromium) through the samples the way a person would:
read them, remove the photo taken twice, fix what a flag points at. It keeps one frame per step, so the GIF shows
what happened rather than how long it took, and the captions carry the real timings. The model is loaded before the
first photo, so those timings are reading alone.
"""

import argparse
import io
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT, CAPTION = 1100, 720, 48
FOCUS_CSS = ".demo-focus { outline: 3px solid #e8a33d !important; outline-offset: 3px; }"
MAX_LOOKS = 3  # yellow rows shown in the GIF; any others stay yellow
LEFT_OUT = {"not_receipt": "not a receipt", "duplicate_photo": "the same photo twice"}


class Recorder:
    def __init__(self, page: Page):
        self.page = page
        self.frames: list[Image.Image] = []
        self.durations: list[int] = []
        self.font = ImageFont.load_default(size=19)

    def shot(self, caption: str, ms: int = 2400) -> None:
        frame = Image.open(io.BytesIO(self.page.screenshot())).convert("RGB")
        canvas = Image.new("RGB", (frame.width, frame.height + CAPTION), "#1f3a2e")
        canvas.paste(frame)
        caption = caption.replace("₩", "KRW ")  # Pillow's built-in font has no won sign
        while self.font.getlength(caption) > frame.width - 36:
            caption = caption[:-2].rstrip() + "…"
        ImageDraw.Draw(canvas).text((18, frame.height + CAPTION // 2), caption, font=self.font, fill="white", anchor="lm")
        self.frames.append(canvas)
        self.durations.append(ms)
        print(f"  frame {len(self.frames)}: {caption}", flush=True)

    def save(self, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        frames = [f.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in self.frames]
        frames[0].save(out, save_all=True, append_images=frames[1:], duration=self.durations, loop=0, optimize=True)


def launch(p, browser: str | None):
    if browser:
        return p.chromium.launch(executable_path=browser)
    try:
        return p.chromium.launch(channel="chrome")
    except PlaywrightError:
        return p.chromium.launch()  # Playwright's Chromium, if `playwright install chromium` was run


def load_model(base: str) -> float | None:
    """Loads the model the app uses, through the app's own Ollama address, and says how long that took."""
    with urllib.request.urlopen(urljoin(base, "healthz"), timeout=10) as resp:
        health = json.load(resp)
    request = urllib.request.Request(
        health["ollama"].rstrip("/") + "/api/generate",
        data=json.dumps({"model": health["model"]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=600) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        print(f"  could not load the model ahead ({exc.code}); the first photo's time includes it", flush=True)
        return None
    return round(time.perf_counter() - started, 1)


def money(amount: float, currency: str) -> str:
    return f"{amount:,.2f}" if currency == "USD" else f"{amount:,.0f}"


def misread(read: list[float], printed: list[float]) -> list[tuple[float, float]]:
    """Each breakdown amount the receipt doesn't print, with the printed amount it most likely is."""
    left = [abs(amount) for amount in printed]
    pairs = []
    for amount in (abs(a) for a in read):
        match = next((p for p in left if abs(p - amount) < 0.005), None)
        if match is not None:
            left.remove(match)
        elif left:
            pairs.append((amount, min(left, key=lambda p: abs(p - amount))))
    return pairs


def items(page: Page) -> list[dict]:
    # The page keeps what it read in `items`; reading it beats scraping the inputs back out of the table.
    return page.evaluate("items.map((i) => ({ key: String(i.key), row: i.row }))")


def focus(page: Page, selector: str) -> None:
    page.evaluate("(s) => document.querySelector(s).classList.add('demo-focus')", selector)


def unfocus(page: Page) -> None:
    page.evaluate("document.querySelectorAll('.demo-focus').forEach((e) => e.classList.remove('demo-focus'))")


def scroll_to(page: Page, selector: str, block: str = "center") -> None:
    page.evaluate("([s, b]) => document.querySelector(s).scrollIntoView({ block: b })", [selector, block])
    page.wait_for_timeout(150)


def chips(page: Page, key: str) -> list[str]:
    return page.locator(f'tr[data-key="{key}"] .chip').all_inner_texts()


def totals(page: Page) -> str:
    return " ".join(page.locator("#totals > *").all_inner_texts())


def wait_for_reads(page: Page, count: int, timeout_ms: int) -> None:
    """Waits until `count` photos are through (read, or refused), and the table is drawn."""
    page.wait_for_function(
        "(n) => (done >= n && (n < queued || !busy)) || !document.querySelector('#error').hidden",
        arg=count,
        timeout=timeout_ms,
    )
    if page.locator("#error").is_visible():
        raise SystemExit(f"The app says: {page.locator('#error').inner_text()}")
    page.wait_for_timeout(400)


def record(base: str, out: Path, browser_path: str | None, per_photo_s: int) -> dict:
    truth = json.loads((ROOT / "samples" / "truth.json").read_text(encoding="utf-8"))
    load_seconds = load_model(base)
    if load_seconds is not None:
        print(f"  model loaded in {load_seconds:.1f} s", flush=True)
    with sync_playwright() as p:
        browser = launch(p, browser_path)
        context = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT}, locale="en-US", color_scheme="light", accept_downloads=True
        )
        context.add_init_script("try { localStorage.setItem('jeongsan-lang', 'en') } catch {}")
        page = context.new_page()
        page.goto(base)
        page.wait_for_function("() => !document.querySelector('#footer').textContent.includes('…')")
        page.add_style_tag(content=FOCUS_CSS)
        model = page.evaluate("model")
        photos = len(page.evaluate("fetch('/api/samples').then((r) => r.json()).then((j) => j.photos)"))
        rec = Recorder(page)

        focus(page, "#sample")
        rec.shot("A month of receipt photos, and an open model on this computer to read them", 3000)
        unfocus(page)

        page.click("#sample")
        wait_for_reads(page, 1, per_photo_s * 1000)
        scroll_to(page, "#progress", "start")
        rec.shot(f"{model} reads them one photo at a time, through Ollama")
        wait_for_reads(page, photos // 2, per_photo_s * 1000 * photos)
        scroll_to(page, "#progress", "start")
        rec.shot(f"Reading {photos} photos on this machine. The waits are cut short in this GIF")
        wait_for_reads(page, photos, per_photo_s * 1000 * photos)

        read = [item["row"] for item in items(page)]
        seconds = [row["seconds"] for row in read]
        stats = page.locator("#stats li").all_inner_texts()
        before = totals(page)
        looks = page.evaluate("[...document.querySelectorAll('#rows tr.look')].map((tr) => tr.dataset.key)")
        picked = {0: "found nothing to look at", 1: "picked 1 yellow row"}.get(
            len(looks), f"picked {len(looks)} yellow rows"
        )
        scroll_to(page, f'tr[data-key="{looks[0]}"]' if looks else "#summary", "start")  # the yellow rows, not the top
        rec.shot(f"All {photos} read, {sum(seconds) / len(seconds):.1f} s a photo. Plain checks, no AI, {picked}", 3600)

        actions = []
        for key in looks[:MAX_LOOKS]:
            row = next(item["row"] for item in items(page) if item["key"] == key)
            flags = chips(page, key)
            selector = f'tr[data-key="{key}"]'
            want = truth.get(row["file_name"], {})
            if "possible_duplicate" in row["flags"]:
                scroll_to(page, selector)
                focus(page, f"{selector} .remove")
                rec.shot("Same date, minute and amount as another photo: the receipt was photographed twice", 3200)
                page.click(f"{selector} .remove")
                page.wait_for_timeout(500)
                actions.append({"file": row["file_name"], "flags": flags, "did": "removed the second photo"})
                scroll_to(page, "#summary", "start")
                rec.shot(f"Removed the copy. The total is now {totals(page)}")
                continue
            scroll_to(page, selector)
            focus(page, selector)
            rec.shot("Yellow: " + "; ".join(flags), 3200)
            unfocus(page)
            if {"bad_business_number", "no_business_number"} & set(row["flags"]) and want.get("business_number"):
                field = page.locator(f'{selector} [data-field="business_number"]')
                field.fill(want["business_number"])
                field.press("Tab")
                page.evaluate("document.activeElement.blur()")
                page.wait_for_timeout(500)
                scroll_to(page, selector)
                left = chips(page, key)
                result = "the check digit passes" if not left else "still flagged: " + "; ".join(left)
                rec.shot(f"Typed the number from the photo, {want['business_number']}: {result}", 3200)
                actions.append({"file": row["file_name"], "flags": flags, "did": f"typed {want['business_number']}",
                                "still_flagged": left})
            elif "sum_mismatch" in row["flags"] and "total" in want:
                currency = row["currency"]
                if row["total"] is None or abs(row["total"] - want["total"]) >= 0.005:
                    field = page.locator(f'{selector} [data-field="total"]')
                    field.fill(money(want["total"], currency))
                    field.press("Tab")
                    page.evaluate("document.activeElement.blur()")
                    page.wait_for_timeout(500)
                    scroll_to(page, selector)
                    left = chips(page, key)
                    result = "the numbers add up" if not left else "still flagged: " + "; ".join(left)
                    rec.shot(f"Typed the total from the photo, {money(want['total'], currency)}: {result}", 3200)
                    actions.append({"file": row["file_name"], "flags": flags, "did": "typed the total",
                                    "still_flagged": left})
                    continue
                wrong = misread([line["amount"] for line in row["breakdown"]], want.get("breakdown", []))
                page.click(f"{selector} img.thumb")
                page.wait_for_timeout(600)
                total = money(row["total"], currency)
                found = "; ".join(f"{money(p, currency)} where the model read {money(r, currency)}" for r, p in wrong)
                rec.shot(f"The photo says {found}. The total, {total}, is right" if found
                         else f"The photo's total, {total}, is right", 4200)
                page.keyboard.press("Escape")
                page.check(f'{selector} [data-field="reviewed"]')
                page.wait_for_timeout(500)
                scroll_to(page, selector)
                rec.shot(f"So the row gets ticked Checked and keeps {total}", 3200)
                actions.append({"file": row["file_name"], "flags": flags, "did": "ticked Checked after the photo",
                                "misread": wrong})
            else:
                actions.append({"file": row["file_name"], "flags": flags, "did": "left yellow"})

        left_out = [
            f"{row['file_name']} ({LEFT_OUT[flag]})" for row in read for flag in row["flags"] if flag in LEFT_OUT
        ]
        if left_out:
            scroll_to(page, "#excluded-card")
            rec.shot("Left out of the totals and listed separately: " + ", ".join(left_out))

        scroll_to(page, "#summary", "start")
        focus(page, "#xlsx")
        after = totals(page)
        rec.shot(f"{after}. The Excel file keeps real dates and live SUMIFS totals", 4200)
        unfocus(page)
        with page.expect_download() as download:
            page.click("#xlsx")
        with tempfile.TemporaryDirectory() as tmp:
            saved = Path(tmp) / download.value.suggested_filename
            download.value.save_as(saved)
            xlsx_bytes = saved.stat().st_size
        browser.close()

    rec.save(out)
    return {
        "model": model,
        "model_load_seconds": load_seconds,
        "photos": photos,
        "seconds_per_photo": round(sum(seconds) / len(seconds), 1),
        "seconds_min_max": [min(seconds), max(seconds)],
        "stats_after_reading": stats,
        "totals_after_reading": before,
        "totals_at_the_end": after,
        "flags": {row["file_name"]: row["flags"] for row in read if row["flags"]},
        "yellow_rows": len(looks),
        "shown_in_gif": actions,
        "left_out": left_out,
        "xlsx_bytes": xlsx_bytes,
        "gif": str(out),
        "gif_frames": len(rec.frames),
        "gif_bytes": out.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000/")
    parser.add_argument("--out", default=str(ROOT / "docs" / "demo.gif"))
    parser.add_argument("--browser", help="path to a Chrome or Chromium binary, if neither is found on its own")
    parser.add_argument("--per-photo", type=int, default=120, help="seconds to wait for one photo before giving up")
    args = parser.parse_args()
    try:
        result = record(args.url, Path(args.out), args.browser, args.per_photo)
    except urllib.error.URLError as exc:
        print(f"Could not reach the app or load its model through Ollama: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
