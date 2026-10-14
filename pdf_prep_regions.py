"""Optional, local-only PaddleOCR figure protection for pdf-prep."""

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def checksum(path):
    digest = hashlib.md5()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def page_numbers(selection, count):
    if selection is None:
        return list(range(1, count + 1))
    numbers = set()
    try:
        for part in selection.split(","):
            endpoints = part.strip().split("-")
            if len(endpoints) == 1:
                first = last = int(endpoints[0])
            elif len(endpoints) == 2:
                first, last = map(int, endpoints)
            else:
                raise ValueError
            if not 1 <= first <= last <= count:
                raise ValueError
            numbers.update(range(first, last + 1))
    except ValueError:
        raise ValueError("--pages must contain valid 1-based pages or ranges, e.g. 1,20-25")
    return sorted(numbers)


def filter_contents_regions(text, rectangles):
    """Reject page-sized layout false positives on confidently recognized contents."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    heading = any(re.fullmatch(r"(?:[ivxlcdm]+\s+)?(?:table\s+of\s+)?contents(?:\s+[ivxlcdm]+)?",
                              line, re.IGNORECASE)
                  for line in lines[:10])
    page_references = len(re.findall(r"(?<![\w.])\d{1,4}(?![\w.])", text))
    entries = len(re.findall(r"(?<![\w.])(?:\d{1,3}\.\d+\b|chapter\s+\d+\b|appendix\s+[a-z]\b)",
                             text, re.IGNORECASE))
    if not heading or len(text.split()) < 80 or page_references < 10 or entries < 8:
        return rectangles
    return [rectangle for rectangle in rectangles
            if not (rectangle[2] - rectangle[0] >= 0.6
                    and rectangle[3] - rectangle[1] >= 0.55
                    and (rectangle[2] - rectangle[0]) * (rectangle[3] - rectangle[1]) >= 0.45)]


def unexplained_graphics(ink, text_height):
    """Do not let a small diagram disappear inside a much larger text block."""
    import numpy as np

    active = ink.copy()
    height, width = active.shape
    for seed_y, seed_x in np.argwhere(active):
        if not active[seed_y, seed_x]:
            continue
        pending = [(int(seed_y), int(seed_x))]
        active[seed_y, seed_x] = False
        left = right = int(seed_x)
        top = bottom = int(seed_y)
        area = 0
        while pending:
            current_y, current_x = pending.pop()
            area += 1
            left, right = min(left, current_x), max(right, current_x)
            top, bottom = min(top, current_y), max(bottom, current_y)
            if (max(right - left + 1, bottom - top + 1) >= max(6, text_height * 0.8)
                    or area >= max(9, text_height ** 2 * 0.1)):
                return True
            for next_y in range(max(0, current_y - 1), min(height, current_y + 2)):
                for next_x in range(max(0, current_x - 1), min(width, current_x + 2)):
                    if active[next_y, next_x]:
                        active[next_y, next_x] = False
                        pending.append((next_y, next_x))
    return False


def filter_text_regions(page, rectangles):
    """Reject verified text blocks and chapter-number badges, not uncertain artwork."""
    import fitz
    import numpy as np
    from PIL import Image, ImageOps

    text = page.get_text()
    rectangles = filter_contents_regions(text, rectangles)
    if not rectangles:
        return rectangles
    words = [(fitz.Rect(word[:4]) * page.rotation_matrix, word[4], word[5:7])
             for word in page.get_text("words") if word[2] > word[0] and word[3] > word[1]]
    if not words:
        return rectangles
    chapter = re.search(r"(?m)^\s*(\d{1,2})\.0\s*\n\s*INTRODUCTION\b", text, re.IGNORECASE)
    median_height = float(np.median([bounds.height for bounds, _, _ in words]))
    rendered = None
    retained = []
    for rectangle in rectangles:
        bounds = fitz.Rect(rectangle[0] * page.rect.width, rectangle[1] * page.rect.height,
                           rectangle[2] * page.rect.width, rectangle[3] * page.rect.height)
        inside = [(box, word, row) for box, word, row in words
                  if bounds.contains((box.tl + box.br) / 2)]
        alphabetic = sum(len(word) >= 3 and word.isalpha() for _, word, _ in inside)
        dense = (alphabetic >= 40 and len({tuple(row) for _, _, row in inside}) >= 10
                 and sum(sum(character.isalpha() for character in word)
                         for _, word, _ in inside) >= 200)
        if any(re.fullmatch(r"(?:fig(?:ure)?\.?|table)", word, re.IGNORECASE)
               and box.x0 < bounds.x1 and box.x1 > bounds.x0
               and bounds.y0 <= box.y0 <= bounds.y1 + median_height * 3
               for box, word, _ in words):
            dense = False
        badge = (chapter is not None and len(text.split()) >= 80
                 and 0 <= rectangle[0] < 0.25 and 0.02 <= rectangle[1] < 0.3
                 and rectangle[2] <= 0.3 and rectangle[3] <= 0.4
                 and 0.65 <= bounds.width / max(1, bounds.height) <= 1.5
                 and any(box.x0 >= bounds.x1 - 2 and box.y0 < bounds.y1
                         and box.y1 > bounds.y0 and box.height >= median_height * 2
                         for box, _, _ in words)
                 and shutil.which("tesseract") is not None)
        if not dense and not badge:
            retained.append(rectangle)
            continue
        try:
            if rendered is None:
                if page.rect.width * page.rect.height * (200 / 72) ** 2 > 16_000_000:
                    raise ValueError("text validation raster exceeds its safety limit")
                pixels = page.get_pixmap(dpi=200, colorspace=fitz.csRGB, alpha=False, annots=False)
                rendered = np.frombuffer(pixels.samples, dtype=np.uint8).reshape(
                    pixels.height, pixels.width, 3)
            height, width = rendered.shape[:2]
            left = max(0, int(np.floor(rectangle[0] * width)))
            top = max(0, int(np.floor(rectangle[1] * height)))
            right = min(width, int(np.ceil(rectangle[2] * width)))
            bottom = min(height, int(np.ceil(rectangle[3] * height)))
            rgb = rendered[top:bottom, left:right]
            if not rgb.size:
                raise ValueError("empty text validation region")
            grey = rgb.mean(axis=2)
            if dense:
                nearby = grey.astype(np.int16)
                for axis in (0, 1):
                    padding = [(0, 0), (0, 0)]
                    padding[axis] = (25, 25)
                    padded = np.pad(nearby, padding, mode="edge")
                    maximum = np.zeros_like(nearby)
                    for offset in range(51):
                        section = [slice(None), slice(None)]
                        section[axis] = slice(offset, offset + nearby.shape[axis])
                        np.maximum(maximum, padded[tuple(section)], out=maximum)
                    nearby = maximum
                ink = ((grey < nearby - 35)
                       | (rgb.max(axis=2).astype(np.int16) - rgb.min(axis=2) > 45))
                covered = np.zeros(ink.shape, dtype=bool)
                for box, _, _ in words:
                    if not bounds.intersects(box) or box.height > median_height * 2.5:
                        continue
                    expanded = box + (-0.7, -0.7, 0.7, 0.7)
                    start_x = max(0, int(np.floor(expanded.x0 * width / page.rect.width)) - left)
                    start_y = max(0, int(np.floor(expanded.y0 * height / page.rect.height)) - top)
                    end_x = min(right - left, int(np.ceil(expanded.x1 * width / page.rect.width)) - left)
                    end_y = min(bottom - top, int(np.ceil(expanded.y1 * height / page.rect.height)) - top)
                    covered[start_y:end_y, start_x:end_x] = True
                unexplained = ink & ~covered
                if (ink.sum() >= 100 and unexplained.sum() <= ink.sum() * 0.01
                        and unexplained.mean() <= 0.002
                        and not unexplained_graphics(unexplained, median_height * height / page.rect.height)):
                    continue
            if badge:
                saturation = rgb.max(axis=2).astype(np.int16) - rgb.min(axis=2)
                matched = False
                for inset in (0.12, 0.22):
                    inset_y, inset_x = round(grey.shape[0] * inset), round(grey.shape[1] * inset)
                    interior = grey[inset_y:-inset_y, inset_x:-inset_x]
                    if (not interior.size or np.percentile(interior, 90) - np.percentile(interior, 55) >= 20
                            or np.percentile(saturation, 95) >= 25):
                        continue
                    ink = interior < np.median(interior) - 35
                    if 0.03 < ink.mean() < 0.45:
                        image = ImageOps.expand(Image.fromarray(np.where(ink, 0, 255).astype(np.uint8)),
                                                border=20, fill=255)
                        encoded = io.BytesIO()
                        image.save(encoded, "PNG")
                        result = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "10", "tsv"],
                                                input=encoded.getvalue(), capture_output=True,
                                                check=True, timeout=10)
                        recognized = [row for row in csv.DictReader(
                            io.StringIO(result.stdout.decode("utf-8")), delimiter="\t")
                            if row.get("level") == "5" and row.get("text", "").strip()]
                        if (len(recognized) == 1 and recognized[0]["text"].strip() == chapter.group(1)
                                and float(recognized[0]["conf"]) >= 85):
                            matched = True
                            break
                if matched:
                    continue
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
        retained.append(rectangle)
    return retained


def main():
    parser = argparse.ArgumentParser(
        description="Detect protected pictures using an already-downloaded PaddleOCR model.")
    parser.add_argument("input")
    parser.add_argument("output", help="region-map JSON to pass to pdf-prep --protect-regions")
    parser.add_argument("--model-dir", required=True,
                        help="local Paddle model directory; no weights are downloaded")
    parser.add_argument("--model-name", default="PP-DocLayout_plus-L")
    parser.add_argument("--render-proxy", help="optional faster PDF used only for detection; "
                        "the map still belongs to the original input")
    parser.add_argument("--pages", help="optional 1-based selection, e.g. 1,20-25")
    parser.add_argument("--text-only-pages", help="known text-only PDF pages to exclude from picture detection, "
                        "e.g. 53,60-62; stored as manual overrides in the map")
    parser.add_argument("--dpi", type=int, default=100)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--threshold", type=float, default=0.25)
    parser.add_argument("--padding", type=float, default=0.005,
                        help="extra margin as a fraction of each page dimension")
    args = parser.parse_args()
    if not Path(args.input).is_file() or not Path(args.model_dir).is_dir():
        parser.error("the input PDF and a local model directory must exist")
    if args.render_proxy and not Path(args.render_proxy).is_file():
        parser.error("the rendering proxy PDF must exist")
    model_dir = Path(args.model_dir)
    if (not (model_dir / "inference.pdiparams").is_file()
            or not any((model_dir / name).is_file()
                       for name in ("inference.json", "inference.pdmodel"))):
        parser.error("the model directory must contain local inference weights and a model graph")
    if Path(args.input).resolve() == Path(args.output).resolve():
        parser.error("the output must not overwrite the input PDF")
    if args.render_proxy and Path(args.render_proxy).resolve() == Path(args.output).resolve():
        parser.error("the output must not overwrite the rendering proxy")
    if args.dpi <= 0 or args.threads <= 0 or not 0 < args.threshold <= 1:
        parser.error("dpi and threads must be positive; threshold must be in (0, 1]")
    if not 0 <= args.padding <= 0.1:
        parser.error("padding must be between 0 and 0.1")
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] = "False"
    try:
        import fitz
        import numpy as np
        import pypdfium2
        from paddleocr import LayoutDetection
    except ImportError as exc:
        parser.error("install pdf-prep[layout] in a separate environment: " + str(exc))
    source_md5 = checksum(args.input)
    rendering_path = args.render_proxy or args.input
    rendering_md5 = checksum(rendering_path)
    manifest = {"version": 1, "source_md5": source_md5, "pages": {},
                "model": args.model_name}
    with fitz.open(args.input) as text_document, \
            pypdfium2.PdfDocument(args.input) as original, \
            pypdfium2.PdfDocument(rendering_path) as document:
        if len(original) != len(document):
            parser.error("the rendering proxy must have the original page count and geometry")
        for number in range(len(original)):
            source_page, proxy_page = original[number], document[number]
            try:
                if (source_page.get_size() != proxy_page.get_size()
                        or source_page.get_rotation() != proxy_page.get_rotation()
                        or source_page.get_bbox() != proxy_page.get_bbox()):
                    parser.error("the rendering proxy must have the original page count and geometry")
            finally:
                source_page.close()
                proxy_page.close()
        manifest["page_count"] = len(document)
        try:
            numbers = page_numbers(args.pages, len(document))
            text_only = set(page_numbers(args.text_only_pages, len(document))) if args.text_only_pages is not None else set()
        except ValueError as exc:
            parser.error(str(exc))
        if text_only:
            manifest["text_only_pages"] = sorted(text_only)
            numbers = sorted(set(numbers) | text_only)
        model = LayoutDetection(model_name=args.model_name, model_dir=args.model_dir,
                                device="cpu", enable_mkldnn=False, cpu_threads=args.threads)
        for index, number in enumerate(numbers, 1):
            if number in text_only:
                manifest["pages"][str(number)] = []
                print("layout: page %d (%d/%d), manual text-only override"
                      % (number, index, len(numbers)), file=sys.stderr, flush=True)
                continue
            page = document[number - 1]
            try:
                bitmap = page.render(scale=args.dpi / 72)
                try:
                    rgb = np.array(bitmap.to_pil().convert("RGB"))
                finally:
                    bitmap.close()
            finally:
                page.close()
            height, width = rgb.shape[:2]
            rectangles = []
            for result in model.predict(rgb, threshold=args.threshold):
                for box in result["boxes"]:
                    if box["label"] not in ("image", "figure", "chart", "table"):
                        continue
                    left, top, right, bottom = map(float, box["coordinate"])
                    rectangle = [max(0.0, left / width - args.padding),
                                 max(0.0, top / height - args.padding),
                                 min(1.0, right / width + args.padding),
                                 min(1.0, bottom / height + args.padding)]
                    if (rectangle[0] < rectangle[2] and rectangle[1] < rectangle[3]
                            and rectangle not in rectangles):
                        rectangles.append(rectangle)
            filtered = filter_text_regions(text_document[number - 1], rectangles)
            if len(filtered) != len(rectangles):
                manifest.setdefault("rejected_text_regions", {})[str(number)] = [
                    rectangle for rectangle in rectangles if rectangle not in filtered]
                print("layout: page %d, ignored %d verified text/decorative regions"
                      % (number, len(rectangles) - len(filtered)), file=sys.stderr, flush=True)
            rectangles = filtered
            manifest["pages"][str(number)] = rectangles
            print("layout: page %d (%d/%d), %d protected regions"
                  % (number, index, len(numbers), len(rectangles)), file=sys.stderr, flush=True)
    if checksum(args.input) != source_md5 or checksum(rendering_path) != rendering_md5:
        sys.exit("pdf-prep-regions: the input changed during detection; map not saved")
    destination = Path(args.output)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json",
                                         dir=destination.resolve().parent, delete=False) as output:
            temporary = output.name
            json.dump(manifest, output, indent=2)
            output.write("\n")
        os.replace(temporary, destination)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    print(args.output)


if __name__ == "__main__":
    main()
