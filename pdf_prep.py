"""Make a scanned PDF pleasant to read: white paper, fast scrolling, selectable text.

Works out what the file actually needs rather than applying everything blindly.

Internet Archive scans layer each page as a low-resolution RGB background (the paper,
plus any continuous-tone photo) with the ink painted over it through a high-resolution
JBIG2 stencil mask. Both colour layers can carry picture detail, so neither may be
downsampled safely. PDF clipping retains compressed images while whitening outside
figures. Raster transcoding is optional and can greatly enlarge a PDF.
DuXiu scans are a single bitonal image per page: already white,
already fast, and usually just missing OCR.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zlib

import fitz
import numpy as np
import pikepdf
from PIL import Image, ImageFilter

from pdf_prep_edges import edge_rectangles
from pdf_prep_regions import filter_text_regions, page_numbers

PAPER_FLOOR = 40     # a channel's paper level cannot sensibly fall below this
MIN_BG_DIM = 1500    # a "background" wider than this is really a full-page image
TEXT_PER_PAGE = 50   # a page with fewer characters than this counts as untexted
SIDEWAYS_SHARE = 0.6  # of a page's read characters, on lines that run up or down the sheet
SIDEWAYS_CHARS = 24   # fewer than this is a turned label on a level page, not a sideways page
SIDEWAYS_COLUMNS = 2  # one sideways column is an axis label beside a level figure
COLUMN_GAP = 6        # points across the sheet between two separate sideways columns
SAME_WORDS = 0.5      # of the words read sideways, found again once the page is level
LEVEL_GAIN = 3        # times more level lettering once turned than the page had as it stood
PAGE_DRAWING_KEYS = ("/Contents", "/Resources", "/MediaBox", "/CropBox", "/BleedBox",
                     "/TrimBox", "/ArtBox", "/UserUnit")
SIOYEK_DB = os.environ.get("PDF_PREP_SIOYEK_DB",
                           os.path.expanduser("~/.local/share/sioyek/shared.db"))

# Every one of these columns holds a document checksum, not a filesystem path.
SIOYEK_KEYS = (("highlights", "document_path"), ("bookmarks", "document_path"),
               ("marks", "document_path"), ("opened_books", "path"),
               ("links", "src_document"), ("links", "dst_document"))


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sioyek_notes(checksum):
    """How many highlights, bookmarks, marks and links Sioyek holds against a file."""
    if not os.path.exists(SIOYEK_DB):
        return 0
    notes = 0
    con = sqlite3.connect("file:%s?mode=ro" % SIOYEK_DB, uri=True)
    try:
        for table, col in SIOYEK_KEYS:
            if table == "opened_books":    # where the reader stopped, not a note
                continue
            try:
                notes += con.execute("SELECT COUNT(*) FROM %s WHERE %s=?" % (table, col),
                                     (checksum,)).fetchone()[0]
            except sqlite3.Error:
                continue
    finally:
        con.close()
    return notes


def migrate_sioyek(old_sum, new_sum):
    """Re-point Sioyek's annotations at the rewritten file.

    Sioyek identifies a document by the MD5 of its entire contents, so *any* edit
    orphans its highlights, bookmarks and marks even though the pages are
    unchanged. Nothing here alters page geometry -- only the images inside each
    page -- so the stored coordinates stay valid and just need re-keying. The one
    pass that would, turning a sideways page level, is not done to a file that
    has notes (see sioyek_notes).
    """
    if old_sum == new_sum or not os.path.exists(SIOYEK_DB):
        return {}
    moved = {}
    con = sqlite3.connect(SIOYEK_DB)
    try:
        for table, col in SIOYEK_KEYS:
            try:
                n = con.execute("SELECT COUNT(*) FROM %s WHERE %s=?" % (table, col),
                                (old_sum,)).fetchone()[0]
            except sqlite3.Error:
                continue
            if not n:
                continue
            # OR REPLACE: opened_books.path and marks are UNIQUE, so a row already
            # keyed to the new checksum would otherwise abort the update.
            con.execute("UPDATE OR REPLACE %s SET %s=? WHERE %s=?" % (table, col, col),
                        (new_sum, old_sum))
            moved["%s.%s" % (table, col)] = n
        con.commit()
    finally:
        con.close()
    return moved


# ------------------------------------------------------------------- analysis

def survey(path):
    """Report what this file is and what it needs."""
    doc = fitz.open(path)
    pages = len(doc)
    texted = 0
    mrc = 0
    fat_fg = 0
    dpis = []
    tinted = 0
    compressible = False
    sampled = 0
    step = max(1, pages // 40)
    for pno in range(pages):
        page = doc[pno]
        if len(page.get_text().strip()) >= TEXT_PER_PAGE:
            texted += 1
        if pno % step:
            continue
        sampled += 1
        imgs = page.get_images(full=True)
        if any(image[4] == 8 and image[8] == "FlateDecode"
               and image[2] * image[3] > 1 for image in imgs):
            compressible = True
        if not imgs:
            continue
        if len(imgs) >= 2 and min(i[2] for i in imgs) < MIN_BG_DIM:
            mrc += 1
        # get_images tuples are (xref, smask, width, height, ...): a wide image
        # carrying an image mask is the costly JPEG 2000 colour layer. If it has
        # already been shrunk there is no speed left to win.
        if any(i[1] != 0 and i[2] >= MIN_BG_DIM for i in imgs):
            fat_fg += 1
        big = max(imgs, key=lambda i: i[2] * i[3])
        width_in = page.rect.width / 72.0
        if width_in > 0:
            dpis.append(big[2] / width_in)
        pix = page.get_pixmap(dpi=36)
        a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)[..., :3].astype(np.float32)
        # Select the paper relative to this page, not at a fixed level: a dark
        # scan can have no pixel above an absolute cut, which silently skipped
        # the very books that most needed whitening.
        luminance = a.mean(axis=2)
        m = luminance > np.percentile(luminance, 70)
        if m.sum() >= 100:
            paper = [float(a[..., c][m].mean()) for c in range(3)]
            # Dim-but-neutral is just dense text dragging the average down; a
            # spread between channels is a real tint.
            if max(paper) - min(paper) > 3 or min(paper) < 235:
                tinted += 1
    doc.close()
    return {
        "pages": pages,
        "texted": texted,
        "needs_ocr": texted < pages * 0.5,
        "is_mrc": bool(sampled and mrc > sampled * 0.5),
        "needs_fast": bool(sampled and fat_fg > sampled * 0.5),
        "dpi": int(np.median(dpis)) if dpis else 0,
        "needs_whiten": bool(sampled and tinted > sampled * 0.2),
        "needs_compress": compressible,
    }


# ------------------------------------------------------------------ whitening

def paper_colour(rgb):
    """Return a dominant paper colour, or decline to clean uncertain artwork."""
    grey = rgb.mean(axis=2).astype(np.uint8)
    histogram = np.bincount(grey.ravel(), minlength=256)
    mode = int(np.argmax(histogram[PAPER_FLOOR:])) + PAPER_FLOOR
    selected = np.abs(grey.astype(np.int16) - mode) <= 12
    if mode < 120 or selected.mean() < 0.3:
        return None
    colour = np.median(rgb[selected], axis=0)
    if colour.max() - colour.min() > 60:
        return None
    return colour


def picture_regions(rgb, colour, excluded_regions=()):
    """Conservatively retain connected dark or coloured areas of an MRC background."""
    height, width = rgb.shape[:2]
    scale = min(1.0, 256.0 / max(width, height))
    thumbnail = Image.fromarray(rgb).resize(
        (max(1, round(width * scale)), max(1, round(height * scale))), Image.BOX)
    small = np.asarray(thumbnail.filter(ImageFilter.MedianFilter(3)), dtype=np.int16)
    grey = small.mean(axis=2)
    saturation = small.max(axis=2) - small.min(axis=2)
    nearby_paper = np.asarray(Image.fromarray(grey.astype(np.uint8)).filter(
        ImageFilter.MaxFilter(31)), dtype=np.int16)
    active = ((grey < nearby_paper - 18)
              | (saturation > colour.max() - colour.min() + 25))
    small_height, small_width = active.shape
    for left, top, right, bottom in excluded_regions:
        start_x = max(0, int(np.floor(left * small_width / width)))
        start_y = max(0, int(np.floor(top * small_height / height)))
        end_x = min(small_width, int(np.ceil(right * small_width / width)))
        end_y = min(small_height, int(np.ceil(bottom * small_height / height)))
        active[start_y:end_y, start_x:end_x] = False
    border_y = max(1, round(small_height * 0.03))
    border_x = max(1, round(small_width * 0.03))
    active[:border_y] = False
    active[-border_y:] = False
    active[:, :border_x] = False
    active[:, -border_x:] = False
    regions = []
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
            for next_y in range(max(0, current_y - 1), min(small_height, current_y + 2)):
                for next_x in range(max(0, current_x - 1), min(small_width, current_x + 2)):
                    if active[next_y, next_x]:
                        active[next_y, next_x] = False
                        pending.append((next_y, next_x))
        if area < 4 or min(right - left + 1, bottom - top + 1) < 2:
            continue
        region_width, region_height = right - left + 1, bottom - top + 1
        vertical_edge = (left < small_width * 0.08 or right > small_width * 0.92)
        horizontal_edge = (top < small_height * 0.08 or bottom > small_height * 0.92)
        if (vertical_edge and ((region_width < small_width * 0.15
                               and region_height > small_height * 0.35)
                              or (region_width < small_width * 0.04
                                  and region_height > small_height * 0.15))):
            continue
        if (horizontal_edge and region_height < small_height * 0.12
                and region_width > small_width * 0.35):
            continue
        regions.append((max(0, int((left - 1) * width / small_width)),
                        max(0, int((top - 1) * height / small_height)),
                        min(width, int(np.ceil((right + 2) * width / small_width))),
                        min(height, int(np.ceil((bottom + 2) * height / small_height)))))
    return regions


def protected_pixels(rgb, regions):
    protected = np.zeros(rgb.shape[:2], dtype=bool)
    for left, top, right, bottom in regions:
        protected[top:bottom, left:right] = True
    return protected


def load_regions(path, checksum, pages, include_overrides=False):
    with open(path, encoding="utf-8") as source:
        manifest = json.load(source)
    if not isinstance(manifest, dict):
        raise ValueError("region map must be a JSON object")
    if (type(manifest.get("version")) is not int or manifest.get("version") != 1
            or manifest.get("source_md5") != checksum
            or manifest.get("page_count") != pages):
        raise ValueError("region map does not match this input PDF")
    regions = manifest.get("pages")
    if not isinstance(regions, dict):
        raise ValueError("region map must contain a pages object")
    rejected = manifest.get("rejected_text_regions", {})
    if not isinstance(rejected, dict):
        raise ValueError("rejected_text_regions must be a pages object")
    regions = dict(regions)
    for page_number, rectangles in rejected.items():
        if not isinstance(rectangles, list) or not isinstance(regions.get(page_number, []), list):
            raise ValueError("page regions must be a list")
        regions[page_number] = list(regions.get(page_number, [])) + rectangles
    for page_number, rectangles in regions.items():
        if (not page_number.isdigit() or not 1 <= int(page_number) <= pages
                or page_number != str(int(page_number))):
            raise ValueError("invalid page number in region map")
        if not isinstance(rectangles, list):
            raise ValueError("page regions must be a list")
        for rectangle in rectangles:
            if (not isinstance(rectangle, list) or len(rectangle) != 4
                    or not all(type(value) in (int, float) and 0 <= value <= 1
                               for value in rectangle)
                    or rectangle[0] >= rectangle[2] or rectangle[1] >= rectangle[3]):
                raise ValueError("regions must be normalized [left, top, right, bottom] rectangles")
    text_only = manifest.get("text_only_pages", [])
    if (not isinstance(text_only, list)
            or any(type(number) is not int or not 1 <= number <= pages for number in text_only)):
        raise ValueError("text_only_pages must be a list of valid 1-based PDF page numbers")
    text_only = set(text_only)
    regions = dict(regions)
    for number in text_only:
        regions[str(number)] = []
    return (regions, text_only) if include_overrides else regions


def content_placements(pdf_page, page, safe_clip=False):
    placements = {}

    def visit(contents, resources, transform, ancestors):
        spaces = resources.get("/ColorSpace", {})
        if safe_clip and (resources.get("/ExtGState")
                          or any(name in spaces for name in ("/DefaultRGB", "/DefaultGray", "/DefaultCMYK"))):
            raise ValueError("clipping leaves complex colour/transparency contexts unchanged")
        stack = []
        for operands, operator in pikepdf.parse_content_stream(contents):
            operation = str(operator)
            if operation == "q":
                stack.append(transform)
            elif operation == "Q":
                if not stack:
                    raise ValueError("unbalanced image graphics state")
                transform = stack.pop()
            elif operation == "cm":
                transform = fitz.Matrix(*map(float, operands)) * transform
            elif operation == "Do":
                obj = resources.get("/XObject", {}).get(operands[0])
                if not isinstance(obj, pikepdf.Stream):
                    continue
                if obj.get("/Subtype") == pikepdf.Name.Image:
                    image_transform = fitz.Matrix(1, 0, 0, -1, 0, 1) * transform * page.transformation_matrix
                    rectangle = fitz.Rect(0, 0, 1, 1) * image_transform
                    placements.setdefault(obj.objgen[0], []).append((rectangle, image_transform))
                elif obj.get("/Subtype") == pikepdf.Name.Form:
                    if obj.objgen in ancestors or len(ancestors) >= 64:
                        raise ValueError("cyclic or excessively nested image forms")
                    matrix = fitz.Matrix(*map(float, obj.get("/Matrix", [1, 0, 0, 1, 0, 0])))
                    visit(obj, obj.get("/Resources", resources), matrix * transform, ancestors | {obj.objgen})

    visit(pdf_page, pdf_page.get("/Resources", {}), fitz.Matrix(1, 0, 0, 1, 0, 0), set())
    return placements


def image_regions(page, xref, rectangles, width, height, placements=None):
    mapped = []
    if not rectangles:
        return mapped
    if placements is None:
        placements = page.get_image_rects(xref, transform=True)
    if not placements:
        raise ValueError("cannot locate the background image on the page")
    for _, transform in placements:
        if abs(transform.a * transform.d - transform.b * transform.c) < 1e-8:
            raise ValueError("background image has a singular transform")
        inverse = ~transform
        for left, top, right, bottom in rectangles:
            rectangle = fitz.Rect(left * page.rect.width, top * page.rect.height,
                                  right * page.rect.width, bottom * page.rect.height)
            rectangle = rectangle * page.derotation_matrix * inverse
            rectangle = rectangle & fitz.Rect(0, 0, 1, 1)
            if rectangle.is_empty:
                continue
            mapped.append((max(0, int(np.floor(rectangle.x0 * width))),
                           max(0, int(np.floor(rectangle.y0 * height))),
                           min(width, int(np.ceil(rectangle.x1 * width))),
                           min(height, int(np.ceil(rectangle.y1 * height)))))
    return mapped


def filter_image_regions(document, xref, rectangles, width, height, pages):
    """Keep a background candidate if any placement cannot be verified as text."""
    retained = []
    for rectangle in rectangles:
        keep = not pages
        for number in pages:
            try:
                page = document[number - 1]
                placements = page.get_image_rects(xref, transform=True)
                normalized = []
                for _, transform in placements:
                    bounds = fitz.Rect(rectangle[0] / width, rectangle[1] / height,
                                       rectangle[2] / width, rectangle[3] / height)
                    bounds = bounds * transform * page.rotation_matrix
                    bounds = bounds & page.rect
                    if not bounds.is_empty:
                        normalized.append([bounds.x0 / page.rect.width, bounds.y0 / page.rect.height,
                                           bounds.x1 / page.rect.width, bounds.y1 / page.rect.height])
                if not normalized or filter_text_regions(page, normalized):
                    keep = True
                    break
            except (ValueError, RuntimeError):
                keep = True
                break
        if keep:
            retained.append(rectangle)
    return retained


def png_flate(image, level=6):
    encoded = io.BytesIO()
    image.save(encoded, "PNG", compress_level=level)
    data = encoded.getvalue()
    chunks = []
    offset = 8
    while offset < len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        if data[offset + 4:offset + 8] == b"IDAT":
            chunks.append(data[offset + 8:offset + 8 + length])
        offset += length + 12
    return b"".join(chunks), pikepdf.Dictionary(
        Predictor=15, Colors=len(image.getbands()), Columns=image.width, BitsPerComponent=8)


def write_background(obj, rgb):
    """Store changed paper losslessly so protected picture pixels remain exact."""
    height, width = rgb.shape[:2]
    flat = np.all(rgb == 255)
    if flat:
        obj.write(zlib.compress(b"\xff"), filter=pikepdf.Name.FlateDecode)
        obj.Width, obj.Height = 1, 1
        obj.ColorSpace = pikepdf.Name.DeviceGray
    else:
        data, parameters = png_flate(Image.fromarray(rgb))
        obj.write(data, filter=pikepdf.Name.FlateDecode)
        obj.Width, obj.Height = width, height
        obj.ColorSpace = pikepdf.Name.DeviceRGB
    obj.BitsPerComponent = 8
    for key in ("/Decode", "/DecodeParms", "/SMask", "/Mask"):
        if key in obj:
            del obj[key]
    if not flat:
        obj.DecodeParms = parameters


def stencil_mask(doc, pdf, image, background):
    mask = pdf.get_object((image[1], 0))
    if (int(mask.get("/Width", 0)) < background[2]
            or int(mask.get("/Height", 0)) < background[3]):
        return False
    if mask.get("/ImageMask") or mask.get("/BitsPerComponent") == 1:
        return True
    if (mask.get("/BitsPerComponent") != 8
            or mask.get("/ColorSpace") != pikepdf.Name.DeviceGray):
        return False
    fitz.TOOLS.mupdf_warnings(reset=True)
    pixels = fitz.Pixmap(doc, image[1])
    warnings = fitz.TOOLS.mupdf_warnings(reset=True)
    if warnings:
        raise ValueError(warnings.replace("\n", "; "))
    samples = np.frombuffer(pixels.samples, dtype=np.uint8)
    return bool(np.all((samples == 0) | (samples == 255)))


def clip_background(pdf, obj, rectangles, width, height):
    """Clip white paper around the untouched original compressed background."""
    resources = pikepdf.Dictionary()
    original = pdf.make_stream(obj.read_raw_bytes())
    for key, value in obj.items():
        if key != "/Length":
            original[key] = value
    resources.Original = original
    commands = ["q /Original Do Q"] if rectangles else []
    commands.append("q 0 0 1 1 re W n")
    for left, top, right, bottom in rectangles:
        commands.append("0 0 1 1 re %.12f %.12f %.12f %.12f re W* n" % (
            left / width, 1 - bottom / height,
            (right - left) / width, (bottom - top) / height))
    commands.append("1 1 1 rg 0 0 1 1 re f Q")
    obj.write(zlib.compress("\n".join(commands).encode("ascii")), filter=pikepdf.Name.FlateDecode)
    for key in list(obj.keys()):
        if key not in ("/Length", "/Filter"):
            del obj[key]
    obj.Type, obj.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
    obj.BBox = pikepdf.Array([0, 0, 1, 1])
    obj.Resources = pikepdf.Dictionary(XObject=resources)
    obj.PDFPrepPaperClip = True


def whiten(path, dst, regions=None, clip=False, text_only_pages=None):
    white = preserved = alone = 0
    jobs = {}
    unsafe = set()
    failures = []
    with fitz.open(path) as doc, pikepdf.open(path) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == pikepdf.Name.Image:
                for key in ("/Mask", "/SMask"):
                    mask = obj.get(key)
                    if isinstance(mask, pikepdf.Stream):
                        unsafe.add(mask.objgen[0])
        for page_number, page in enumerate(doc, 1):
            images = page.get_images(full=True)
            plain = {image[0]: image for image in images if image[1] == 0}
            masked = [image for image in images if image[1] != 0]
            if len(plain) != 1 or not masked or (clip and str(page_number) not in (regions or {})):
                unsafe.update(plain)
                alone += 1
                continue
            xref, background = next(iter(plain.items()))
            obj = pdf.get_object((xref, 0))
            referencer = pdf.get_object((background[-1], 0)) if background[-1] else None
            if referencer is not None and referencer.get("/PDFPrepPaperClip"):
                unsafe.add(xref)
                alone += 1
                continue
            if ("/Mask" in obj or "/SMask" in obj or obj.get("/ImageMask")
                    or int(obj.get("/BitsPerComponent", 8)) != 8
                    or obj.get("/ColorSpace") not in (pikepdf.Name.DeviceRGB,
                                                    pikepdf.Name.DeviceGray)):
                unsafe.add(xref)
                alone += 1
                continue
            try:
                stencil = any(stencil_mask(doc, pdf, image, background) for image in masked)
            except Exception as exc:
                stencil = False
                failures.append((page_number, str(exc)))
            if not stencil:
                unsafe.add(xref)
                alone += 1
                continue
            try:
                placements = (content_placements(pdf.pages[page_number - 1], page, safe_clip=True).get(xref, [])
                              if clip else None)
            except (ValueError, pikepdf.PdfError) as exc:
                unsafe.add(xref)
                alone += 1
                failures.append((page_number, str(exc)))
                continue
            job = jobs.setdefault(xref, {"pages": [], "regions": [], "excluded": []})
            job["pages"].append(page_number)
            rectangles = [] if page_number in (text_only_pages or ()) else (regions or {}).get(str(page_number), [])
            excluded = []
            if rectangles:
                filtered = filter_text_regions(page, rectangles)
                excluded = [rectangle for rectangle in rectangles if rectangle not in filtered]
                rectangles = filtered
            if rectangles or excluded:
                try:
                    job["excluded"].extend(image_regions(page, xref, excluded,
                                                         background[2], background[3], placements))
                    job["regions"].extend(image_regions(page, xref, rectangles,
                                                        background[2], background[3], placements))
                except (ValueError, pikepdf.PdfError) as exc:
                    unsafe.add(xref)
                    failures.append((page_number, str(exc)))
        changed = False
        for xref, job in jobs.items():
            count = len(job["pages"])
            if xref in unsafe:
                alone += count
                continue
            try:
                fitz.TOOLS.mupdf_warnings(reset=True)
                pixmap = fitz.Pixmap(doc, xref)
                if pixmap.colorspace.n != 3:
                    pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
                if pixmap.alpha:
                    pixmap = fitz.Pixmap(pixmap, 0)
                warnings = fitz.TOOLS.mupdf_warnings(reset=True)
                if warnings:
                    raise ValueError(warnings.replace("\n", "; "))
                rgb = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                    pixmap.height, pixmap.width, 3)
                colour = paper_colour(rgb)
                if colour is None:
                    alone += count
                    continue
                excluded = job["excluded"] if len(job["pages"]) == 1 else []
                detected = [] if all(number in (text_only_pages or ()) for number in job["pages"]) else picture_regions(rgb, colour, excluded)
                detected = filter_image_regions(doc, xref, detected, pixmap.width, pixmap.height, job["pages"])
                rectangles = detected + job["regions"]
                protected = protected_pixels(rgb, rectangles)
                if protected.mean() > (0.9 if clip else 0.75):
                    alone += count
                    continue
                if np.all(rgb[~protected] == 255):
                    alone += count
                    continue
            except Exception as exc:
                alone += count
                failures.append((job["pages"][0], str(exc)))
                continue
            if clip:
                clip_background(pdf, pdf.get_object((xref, 0)), rectangles,
                                pixmap.width, pixmap.height)
            else:
                output = np.full_like(rgb, 255)
                output[protected] = rgb[protected]
                write_background(pdf.get_object((xref, 0)), output)
            changed = True
            if protected.any():
                preserved += count
            else:
                white += count
        if changed:
            pdf.save(dst, object_stream_mode=pikepdf.ObjectStreamMode.generate,
                     compress_streams=not clip)
    if not changed:
        shutil.copyfile(path, dst)
    if failures:
        print("whiten: left %d uncertain images unchanged" % len(failures), file=sys.stderr)
        for page_number, reason in failures[:5]:
            print("  page %d: %s" % (page_number, reason), file=sys.stderr)
    return white, preserved, alone


# ----------------------------------------------------------------- speed pass

def image_objects(resources, seen):
    xobjects = resources.get("/XObject", {})
    for obj in dict(xobjects).values():
        if not isinstance(obj, pikepdf.Stream) or obj.objgen in seen:
            continue
        seen.add(obj.objgen)
        if obj.get("/Subtype") == "/Image":
            yield obj
        elif obj.get("/Subtype") == "/Form":
            yield from image_objects(obj.get("/Resources", resources), seen)


def slim_foregrounds(path, max_side=128):
    """Legacy, explicitly lossy shrinking of the MRC foreground colour field.

    Decoding ten megapixels of JPEG 2000 per page is what makes these files crawl.
    The stencil carries the letter shapes, and PDF lets an image mask have different
    pixel dimensions from the image it masks, so the colour field can shrink to a
    thumbnail while the text stays exactly as sharp. Both explicit /Mask streams
    and soft /SMask streams are supported, including images inside Form XObjects.

    Must be pikepdf: PyMuPDF's replace_image drops masks, losing the stencil and
    painting each page a solid black smear.
    """
    slimmed = 0
    failures = []
    seen = set()
    with fitz.open(path) as source, pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            for obj in image_objects(page.get("/Resources", {}), seen):
                mask = obj.get("/SMask")
                if not isinstance(mask, pikepdf.Stream):
                    mask = obj.get("/Mask")
                if not isinstance(mask, pikepdf.Stream):
                    continue
                width, height = int(obj.Width), int(obj.Height)
                if width < MIN_BG_DIM or max(width, height) <= max_side:
                    continue
                try:
                    fitz.TOOLS.mupdf_warnings(reset=True)
                    pixmap = fitz.Pixmap(source, obj.objgen[0])
                    if pixmap.colorspace is None:
                        raise ValueError("foreground has no colour space")
                    if pixmap.colorspace.n != 3:
                        pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
                    if pixmap.alpha:
                        pixmap = fitz.Pixmap(pixmap, 0)
                    image = Image.frombytes("RGB", (pixmap.width, pixmap.height),
                                            pixmap.samples)
                    warnings = fitz.TOOLS.mupdf_warnings(reset=True)
                    if warnings:
                        raise ValueError(warnings.replace("\n", "; "))
                except Exception as exc:
                    failures.append((page_number, obj.objgen[0], str(exc)))
                    continue
                scale = max_side / float(max(width, height))
                small = image.resize((max(1, int(width * scale)),
                                      max(1, int(height * scale))), Image.BILINEAR)
                obj.write(zlib.compress(small.tobytes()),
                          filter=pikepdf.Name("/FlateDecode"))
                obj.Width, obj.Height = small.width, small.height
                obj.ColorSpace = pikepdf.Name("/DeviceRGB")
                obj.BitsPerComponent = 8
                for key in ("/DecodeParms", "/Decode"):
                    if key in obj:
                        del obj[key]
                slimmed += 1
        pdf.save(path, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    if failures:
        print("fast: could not decode %d foreground images; left them unchanged"
              % len(failures), file=sys.stderr)
        for page_number, object_number, reason in failures[:5]:
            print("  page %d, image %d: %s" % (page_number, object_number, reason),
                  file=sys.stderr)
    return slimmed


def merged_regions(rectangles):
    remaining = list(rectangles)
    merged = []
    while remaining:
        left, top, right, bottom = remaining.pop()
        index = 0
        while index < len(remaining):
            other = remaining[index]
            if left <= other[2] and other[0] <= right and top <= other[3] and other[1] <= bottom:
                left, top = min(left, other[0]), min(top, other[1])
                right, bottom = max(right, other[2]), max(bottom, other[3])
                remaining.pop(index)
                index = 0
            else:
                index += 1
        merged.append((left, top, right, bottom))
    return merged


def decoded_rgb(document, xref):
    fitz.TOOLS.mupdf_warnings(reset=True)
    pixmap = fitz.Pixmap(document, xref)
    if pixmap.colorspace is None:
        raise ValueError("image has no colour space")
    if pixmap.colorspace.n != 3:
        pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
    if pixmap.alpha:
        pixmap = fitz.Pixmap(pixmap, 0)
    warnings = fitz.TOOLS.mupdf_warnings(reset=True)
    if warnings:
        raise ValueError(warnings.replace("\n", "; "))
    return np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width, 3)


def native_patch(pdf, rgb, alpha=None, interpolate=False, mask_interpolate=False):
    height, width = rgb.shape[:2]
    image = pdf.make_stream(zlib.compress(rgb.tobytes(), 6))
    image.Type, image.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
    image.Width, image.Height = width, height
    image.ColorSpace, image.BitsPerComponent = pikepdf.Name.DeviceRGB, 8
    image.Filter = pikepdf.Name.FlateDecode
    image.Interpolate = interpolate
    if alpha is not None:
        packed = np.packbits(alpha != 0, axis=1).tobytes()
        mask = pdf.make_stream(zlib.compress(packed, 6))
        mask.Type, mask.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
        mask.Width, mask.Height = width, height
        mask.ImageMask, mask.BitsPerComponent = True, 1
        mask.Decode, mask.Filter = pikepdf.Array([1, 0]), pikepdf.Name.FlateDecode
        mask.Interpolate = mask_interpolate
        image.Mask = mask
    return image


def cropped_layer(pdf, obj, rgb, rectangles, alpha=None, interpolate=False, mask_interpolate=False):
    """Replace an image with native pixel crops, keeping its unit-square placement."""
    height, width = rgb.shape[:2]
    rectangles = merged_regions(rectangles)
    patches = []
    if alpha is not None:
        remaining = alpha.copy()
        for left, top, right, bottom in rectangles:
            remaining[top:bottom, left:right] = 0
        occupied = np.flatnonzero(np.any(remaining, axis=1))
        if occupied.size:
            groups = np.split(occupied, np.flatnonzero(np.diff(occupied) > 17) + 1)
            for group in groups:
                top, bottom = max(0, int(group[0]) - 8), min(height, int(group[-1]) + 9)
                columns = np.flatnonzero(np.any(remaining[top:bottom], axis=0))
                left, right = max(0, int(columns[0]) - 8), min(width, int(columns[-1]) + 9)
                patches.append(((left, top, right, bottom), remaining[top:bottom, left:right], None))
    for left, top, right, bottom in rectangles:
        bounds = (max(0, left - 8), max(0, top - 8), min(width, right + 8), min(height, bottom + 8))
        crop_mask = None if alpha is None else alpha[bounds[1]:bounds[3], bounds[0]:bounds[2]]
        patches.append((bounds, crop_mask, (left, top, right, bottom)))
    resources = pikepdf.Dictionary()
    commands = [] if alpha is not None else ["q 1 1 1 rg 0 0 1 1 re f Q"]
    for number, (bounds, crop_mask, clip) in enumerate(patches):
        left, top, right, bottom = bounds
        name = "/Native%d" % number
        resources[name] = native_patch(pdf, rgb[top:bottom, left:right], crop_mask,
                                      interpolate, mask_interpolate)
        commands.append("q")
        if clip is not None:
            commands.append("%.12f %.12f %.12f %.12f re W n" % (
                clip[0] / width, 1 - clip[3] / height,
                (clip[2] - clip[0]) / width, (clip[3] - clip[1]) / height))
        commands.append("%.12f 0 0 %.12f %.12f %.12f cm %s Do Q" % (
            (right - left) / width, (bottom - top) / height,
            left / width, 1 - bottom / height, name))
    obj.write(zlib.compress("\n".join(commands).encode("ascii")), filter=pikepdf.Name.FlateDecode)
    for key in list(obj.keys()):
        if key not in ("/Length", "/Filter"):
            del obj[key]
    obj.Type, obj.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
    obj.BBox = pikepdf.Array([0, 0, 1, 1])
    obj.Resources = pikepdf.Dictionary(XObject=resources)


def encoded_native_job(arguments):
    path, image, rectangles, crop, settings = arguments
    settings = dict(settings)
    text_only = settings.pop("text_only", False)
    pages = settings.pop("pages", [])
    excluded = settings.pop("excluded", [])
    if len(pages) != 1:
        excluded = []
    try:
        if image[2] * image[3] * 3 > 256 * 1024 ** 2:
            raise ValueError("native image exceeds the 256 MiB decoding safety limit")
        with fitz.open(path) as document, pikepdf.Pdf.new() as temporary:
            rgb = decoded_rgb(document, image[0])
            obj = temporary.make_stream(b"")
            if not image[1]:
                colour = paper_colour(rgb)
                if colour is None:
                    return None, "unchanged", None
                detected = [] if text_only else picture_regions(rgb, colour, excluded)
                detected = filter_image_regions(document, image[0], detected, image[2], image[3], pages)
                rectangles = merged_regions(detected + rectangles)
                protected = protected_pixels(rgb, rectangles)
                if protected.mean() > 0.9:
                    return None, "unchanged", None
                if crop:
                    cropped_layer(temporary, obj, rgb, rectangles, **settings)
                else:
                    output = np.full_like(rgb, 255)
                    output[protected] = rgb[protected]
                    write_background(obj, output)
                    obj.Type, obj.Subtype = pikepdf.Name.XObject, pikepdf.Name.Image
                kind = "pictures" if rectangles else "white"
            else:
                fitz.TOOLS.mupdf_warnings(reset=True)
                pixmap = fitz.Pixmap(document, image[1])
                warnings = fitz.TOOLS.mupdf_warnings(reset=True)
                if warnings or pixmap.n != 1:
                    raise ValueError(warnings or "unsupported stencil channels")
                alpha = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                    pixmap.height, pixmap.width)
                if not np.all((alpha == 0) | (alpha == 255)):
                    raise ValueError("stencil is not bitonal")
                cropped_layer(temporary, obj, rgb, rectangles, alpha, **settings)
                kind = "foregrounds"
            page = temporary.add_blank_page(page_size=(72, 72))
            page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Native=obj))
            encoded = io.BytesIO()
            temporary.save(encoded)
            return encoded.getvalue(), kind, None
    except Exception as exc:
        return None, "unchanged", str(exc)


def prepare_native(path, dst, regions=None, clean=True, crop=False, workers=1, text_only_pages=None):
    """Preserve native foreground pixels, masks and protected background crops."""
    stats = {"foregrounds": 0, "white": 0, "pictures": 0, "unchanged": 0}
    jobs, unsafe, failures = {}, set(), []
    with fitz.open(path) as document, pikepdf.open(path) as pdf:
        for number, page in enumerate(document, 1):
            images = page.get_images(full=True)
            plain = {image[0]: image for image in images if not image[1]}
            masked = {image[0]: image for image in images if image[1]}
            if len(plain) != 1 or len(masked) != 1 or (crop and str(number) not in (regions or {})):
                unsafe.update(image[0] for image in images)
                stats["unchanged"] += 1
                continue
            background = next(iter(plain.values()))
            foreground = next(iter(masked.values()))
            objects = [pdf.get_object((image[0], 0)) for image in (background, foreground)]
            mask = pdf.get_object((foreground[1], 0))
            if (any(obj.get("/ColorSpace") != pikepdf.Name.DeviceRGB
                    or obj.get("/BitsPerComponent") != 8 for obj in objects)
                    or "/Mask" in objects[0] or "/SMask" in objects[0]
                    or foreground[2:4] != (int(mask.Width), int(mask.Height))
                    or not (mask.get("/ImageMask") or mask.get("/BitsPerComponent") == 1)):
                unsafe.update(image[0] for image in images)
                failures.append((number, "not a native-resolution RGB/bitonal MRC pair; use the original PDF"))
                stats["unchanged"] += 1
                continue
            rectangles = [] if number in (text_only_pages or ()) else (regions or {}).get(str(number), [])
            filtered = filter_text_regions(page, rectangles)
            excluded = [rectangle for rectangle in rectangles if rectangle not in filtered]
            rectangles = filtered
            try:
                placements = content_placements(pdf.pages[number - 1], page) if rectangles or excluded else {}
            except ValueError as exc:
                unsafe.update(candidate[0] for candidate in images)
                failures.append((number, str(exc)))
                continue
            for image in (background, foreground):
                job = jobs.setdefault(image[0], {"image": image, "regions": [], "pages": [],
                    "settings": {"interpolate": bool(pdf.get_object((image[0], 0)).get("/Interpolate", False)),
                                 "mask_interpolate": bool(mask.get("/Interpolate", False)) if image[1] else False,
                                 "excluded": []}})
                job["pages"].append(number)
                job["settings"]["pages"] = job["pages"]
                job["settings"]["text_only"] = all(page_number in (text_only_pages or ()) for page_number in job["pages"])
                try:
                    job["regions"].extend(image_regions(page, image[0],
                        rectangles, image[2], image[3], placements.get(image[0], [])))
                    if not image[1]:
                        job["settings"]["excluded"].extend(image_regions(page, image[0],
                            excluded, image[2], image[3], placements.get(image[0], [])))
                except ValueError as exc:
                    unsafe.update(candidate[0] for candidate in images)
                    failures.append((number, str(exc)))
        selected = [(xref, job) for xref, job in jobs.items()
                    if xref not in unsafe and (job["image"][1] or clean)]
        with ProcessPoolExecutor(max_workers=workers) as executor:
            for start in range(0, len(selected), workers * 2):
                batch = selected[start:start + workers * 2]
                arguments = [(path, job["image"], job["regions"], crop, job["settings"]) for _, job in batch]
                for (xref, job), (encoded, kind, reason) in zip(batch, executor.map(encoded_native_job, arguments)):
                    stats[kind] += len(job["pages"])
                    if reason:
                        failures.append((job["pages"][0], reason))
                    if encoded is None:
                        continue
                    with pikepdf.open(io.BytesIO(encoded)) as temporary:
                        replacement = pdf.copy_foreign(temporary.pages[0].Resources.XObject.Native)
                    obj = pdf.get_object((xref, 0))
                    obj.write(replacement.read_raw_bytes(), filter=replacement.get("/Filter"))
                    for key in list(obj.keys()):
                        if key not in ("/Length", "/Filter"):
                            del obj[key]
                    for key, value in replacement.items():
                        if key not in ("/Length", "/Filter"):
                            obj[key] = value
                if start // 50 != (start + len(batch)) // 50:
                    print("native: %d foregrounds, %d backgrounds processed" % (
                        stats["foregrounds"], stats["white"] + stats["pictures"]), flush=True)
        pdf.save(dst, object_stream_mode=pikepdf.ObjectStreamMode.generate)
    if failures:
        print("native: %d unsupported image uses left unchanged" % len(failures), file=sys.stderr)
        for number, reason in failures[:5]:
            print("  page %d: %s" % (number, reason), file=sys.stderr)
    return stats


def image_encodings(image, pixels):
    """Compare fast lossless Flate encodings without changing the image decoder."""
    candidates = [(zlib.compress(pixels, 9), pikepdf.Name.FlateDecode, None)]
    data, parameters = png_flate(image, level=9)
    candidates.append((data, pikepdf.Name.FlateDecode, parameters))
    return candidates


def encoded_black_text_job(arguments):
    path, image, rectangles = arguments
    try:
        if not rectangles:
            return zlib.compress(bytes(3)), 1, 1, None, None
        if image[2] * image[3] * 3 > 256 * 1024 ** 2:
            raise ValueError("native image exceeds the 256 MiB decoding safety limit")
        with fitz.open(path) as document:
            rgb = decoded_rgb(document, image[0])
        if rgb.shape != (image[3], image[2], 3):
            raise ValueError("decoded foreground dimensions differ from the PDF image")
        output = np.zeros_like(rgb)
        for left, top, right, bottom in rectangles:
            left, top = max(0, left - 32), max(0, top - 32)
            right, bottom = min(image[2], right + 32), min(image[3], bottom + 32)
            output[top:bottom, left:right] = rgb[top:bottom, left:right]
        data, parameters = png_flate(Image.fromarray(output))
        return data, image[2], image[3], dict(parameters), None
    except Exception as exc:
        return None, None, None, None, str(exc)


def prepare_black_text(path, dst, regions, workers=1, keep_figure_encoding=False):
    """Blacken text fields, preserving figure pixels or their original encoding."""
    stats = {"black_pages": 0, "figure_pages": 0, "unchanged": 0, "images": 0}
    jobs, unsafe, failures = {}, set(), []
    with fitz.open(path) as document, pikepdf.open(path) as pdf:
        for number, page in enumerate(document, 1):
            images = page.get_images(full=True)
            masked = [image for image in images if image[1]]
            plain = [image for image in images if not image[1]]
            if len(masked) != 1 or len(plain) != 1 or str(number) not in regions:
                unsafe.update(image[0] for image in masked)
                stats["unchanged"] += 1
                continue
            image = masked[0]
            obj = pdf.get_object((image[0], 0))
            mask = obj.get("/Mask")
            if (obj.get("/PDFPrepBlackText") or obj.get("/Filter") != pikepdf.Name.JPXDecode
                    or obj.get("/ColorSpace") != pikepdf.Name.DeviceRGB
                    or obj.get("/BitsPerComponent") != 8 or image[2] < MIN_BG_DIM
                    or any(key in obj for key in ("/SMask", "/Decode", "/DecodeParms", "/SMaskInData"))
                    or not isinstance(mask, pikepdf.Stream)
                    or not mask.get("/ImageMask")
                    or mask.get("/BitsPerComponent", 1) != 1
                    or (int(mask.get("/Width", 0)), int(mask.get("/Height", 0))) != image[2:4]):
                unsafe.add(image[0])
                stats["unchanged"] += 1
                continue
            try:
                placements = content_placements(pdf.pages[number - 1], page, safe_clip=True)
                protected = filter_text_regions(page, regions[str(number)])
                rectangles = image_regions(page, image[0], protected, image[2], image[3],
                                           placements.get(image[0], []))
                rectangles = merged_regions(rectangles)
                if sum((right - left) * (bottom - top) for left, top, right, bottom in rectangles) > image[2] * image[3] * 0.9:
                    unsafe.add(image[0])
                    stats["unchanged"] += 1
                    continue
            except (ValueError, pikepdf.PdfError) as exc:
                unsafe.add(image[0])
                stats["unchanged"] += 1
                failures.append((number, str(exc)))
                continue
            job = jobs.setdefault(image[0], {"image": image, "regions": [], "pages": []})
            job["regions"].extend(rectangles)
            job["pages"].append(number)
        selected = [(xref, job) for xref, job in jobs.items() if xref not in unsafe]
        stats["unchanged"] += sum(len(job["pages"]) for xref, job in jobs.items() if xref in unsafe)
        if keep_figure_encoding:
            stats["figure_pages"] = sum(len(job["pages"]) for _, job in selected if job["regions"])
            selected = [(xref, job) for xref, job in selected if not job["regions"]]
        with ProcessPoolExecutor(max_workers=workers) as executor:
            for start in range(0, len(selected), workers * 2):
                batch = selected[start:start + workers * 2]
                arguments = [(path, job["image"], merged_regions(job["regions"])) for _, job in batch]
                for (xref, job), (data, width, height, parameters, reason) in zip(
                        batch, executor.map(encoded_black_text_job, arguments)):
                    if reason:
                        failures.append((job["pages"][0], reason))
                        stats["unchanged"] += len(job["pages"])
                        continue
                    obj = pdf.get_object((xref, 0))
                    obj.write(data, filter=pikepdf.Name.FlateDecode)
                    obj.Width, obj.Height = width, height
                    if parameters is not None:
                        obj.DecodeParms = pikepdf.Dictionary(parameters)
                    obj.PDFPrepBlackText = True
                    stats["images"] += 1
                    stats["figure_pages" if job["regions"] else "black_pages"] += len(job["pages"])
                if start // 50 != (start + len(batch)) // 50:
                    name = "compact-reading" if keep_figure_encoding else "black-text-fast"
                    print("%s: %d pages prepared" % (name, stats["black_pages"] + stats["figure_pages"]), flush=True)
        if not keep_figure_encoding or stats["images"]:
            pdf.save(dst, object_stream_mode=pikepdf.ObjectStreamMode.generate,
                     compress_streams=not keep_figure_encoding)
    if keep_figure_encoding and not stats["images"]:
        shutil.copyfile(path, dst)
    if failures:
        name = "compact-reading" if keep_figure_encoding else "black-text-fast"
        print("%s: %d unsupported uses left unchanged" % (name, len(failures)), file=sys.stderr)
        for number, reason in failures[:5]:
            print("  page %d: %s" % (number, reason), file=sys.stderr)
    return stats


def edge_cleanup_job(arguments):
    path, number, regions = arguments
    try:
        with fitz.open(path) as document:
            page = document[number - 1]
            if document.xref_get_key(page.xref, "PDFPrepEdgeCleanup")[0] != "null":
                return number, [], None
            rectangles = edge_rectangles(page, regions)
            to_pdf = page.derotation_matrix * ~page.transformation_matrix
            return number, [list(rectangle * to_pdf) for rectangle in rectangles], None
    except Exception as exc:
        return number, [], str(exc)


def clean_edges(path, dst, regions, workers=1):
    """Hide confidently detected edge debris without rewriting existing image streams."""
    stats = {"pages": 0, "rectangles": 0, "unchanged": 0}
    failures = []
    with pikepdf.open(path) as pdf:
        arguments = [(path, number, regions[str(number)]) for number in range(1, len(pdf.pages) + 1)
                     if str(number) in regions]
        with ProcessPoolExecutor(max_workers=workers) as executor:
            for completed, (number, rectangles, reason) in enumerate(
                    executor.map(edge_cleanup_job, arguments, chunksize=4), 1):
                if completed % 128 == 0:
                    print("clean-edges: %d of %d pages checked" % (completed, len(arguments)), flush=True)
                if reason:
                    failures.append((number, reason))
                if not rectangles:
                    continue
                page = pdf.pages[number - 1]
                previous = page.obj.get("/Contents", pikepdf.Array())
                streams = [previous] if isinstance(previous, pikepdf.Stream) else list(previous)
                commands = ["Q", "q", "1 1 1 rg"]
                for left, bottom, right, top in rectangles:
                    commands.append("%.6f %.6f %.6f %.6f re f" % (left, bottom, right - left, top - bottom))
                commands.append("Q")
                overlay = pdf.make_stream(b"")
                overlay.write(zlib.compress("\n".join(commands).encode("ascii")), filter=pikepdf.Name.FlateDecode)
                page.Contents = pikepdf.Array([pdf.make_stream(b"q"), *streams, overlay])
                page.PDFPrepEdgeCleanup = pikepdf.Array(rectangles)
                stats["pages"] += 1
                stats["rectangles"] += len(rectangles)
        stats["unchanged"] = len(pdf.pages) - stats["pages"]
        pdf.save(dst, object_stream_mode=pikepdf.ObjectStreamMode.generate, compress_streams=False)
    if failures:
        print("clean-edges: left %d uncertain pages unchanged" % len(failures), file=sys.stderr)
        for number, reason in failures[:5]:
            print("  page %d: %s" % (number, reason), file=sys.stderr)
    return stats


def compact(path):
    """Re-encode only when lossless candidates save space, never enlarging the PDF."""
    source_bytes = os.path.getsize(path)
    stats = {"images": 0, "streams": 0, "encodings": {},
             "before_bytes": source_bytes, "after_bytes": source_bytes}
    failures = []
    fd, temporary = tempfile.mkstemp(suffix=".pdf", dir=os.path.dirname(os.path.abspath(path)))
    os.close(fd)
    try:
        with pikepdf.open(path) as pdf:
            masks = set()
            for obj in pdf.objects:
                if isinstance(obj, pikepdf.Stream):
                    if (obj.get("/Subtype") == pikepdf.Name.Image
                            and (obj.get("/ImageMask") or obj.get("/BitsPerComponent") == 1)):
                        masks.add(obj.objgen)
                    for key in ("/Mask", "/SMask"):
                        mask = obj.get(key)
                        if isinstance(mask, pikepdf.Stream):
                            masks.add(mask.objgen)
                    parameters = obj.get("/DecodeParms")
                    parameters = list(parameters) if isinstance(parameters, pikepdf.Array) else [parameters]
                    for parameter in parameters:
                        if isinstance(parameter, pikepdf.Dictionary):
                            globals_stream = parameter.get("/JBIG2Globals")
                            if isinstance(globals_stream, pikepdf.Stream):
                                masks.add(globals_stream.objgen)
            for obj in pdf.objects:
                if not isinstance(obj, pikepdf.Stream) or obj.objgen in masks:
                    continue
                if obj.get("/Type") in (pikepdf.Name.ObjStm, pikepdf.Name.XRef,
                                       pikepdf.Name.Metadata):
                    continue
                image = obj.get("/Subtype") == pikepdf.Name.Image
                if image and (obj.get("/ImageMask") or obj.get("/BitsPerComponent") != 8):
                    continue
                if obj.get("/Filter") not in (None, pikepdf.Name.FlateDecode):
                    continue
                try:
                    if int(obj.get("/Length", 0)) > 64 * 1024 * 1024:
                        continue
                    old_data = obj.read_raw_bytes()
                    if len(old_data) < 128:
                        continue
                    if image:
                        space = obj.get("/ColorSpace")
                        if space not in (pikepdf.Name.DeviceRGB, pikepdf.Name.DeviceGray):
                            continue
                        width, height = int(obj.Width), int(obj.Height)
                        mode = "RGB" if space == pikepdf.Name.DeviceRGB else "L"
                        channels = 3 if mode == "RGB" else 1
                        if width * height * channels > 64 * 1024 * 1024:
                            continue
                    pdf.get_warnings()
                    pixels = obj.read_bytes()
                    warnings = pdf.get_warnings()
                    if warnings:
                        raise ValueError("; ".join(warnings))
                    if len(pixels) > 64 * 1024 * 1024:
                        continue
                    if image:
                        if len(pixels) != width * height * channels:
                            raise ValueError("decoded image length does not match its dimensions")
                        raster = Image.frombytes(mode, (width, height), pixels)
                        candidates = image_encodings(raster, pixels)
                    else:
                        candidates = [(zlib.compress(pixels, 9), pikepdf.Name.FlateDecode, None)]
                    data, encoding, parameters = min(
                        candidates, key=lambda candidate: len(candidate[0])
                        + (80 if candidate[2] is not None else 0))
                    cost = len(data) + (80 if parameters is not None else 0)
                    if cost + 64 >= len(old_data):
                        continue
                except Exception as exc:
                    failures.append((obj.objgen[0], str(exc)))
                    continue
                obj.write(data, filter=encoding)
                if "/DecodeParms" in obj:
                    del obj["/DecodeParms"]
                if parameters is not None:
                    obj.DecodeParms = parameters
                stats["images" if image else "streams"] += 1
                label = "PNG-Flate" if parameters is not None else "Flate"
                stats["encodings"][label] = stats["encodings"].get(label, 0) + 1
            if stats["images"] or stats["streams"]:
                compress_streams = all(bool(pdf.get_object(objgen).get("/Filter"))
                                       for objgen in masks)
                pdf.save(temporary, object_stream_mode=pikepdf.ObjectStreamMode.generate,
                         compress_streams=compress_streams)
        if os.path.getsize(temporary) and os.path.getsize(temporary) < source_bytes:
            stats["after_bytes"] = os.path.getsize(temporary)
            os.replace(temporary, path)
        else:
            stats["images"] = stats["streams"] = 0
            stats["encodings"] = {}
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if failures:
        print("compress: left %d uncertain streams unchanged" % len(failures), file=sys.stderr)
        for object_number, reason in failures[:5]:
            print("  object %d: %s" % (object_number, reason), file=sys.stderr)
    return stats


# ------------------------------------------------------------------------ OCR

def run_ocr(src, dst, dpi):
    cmd = ["ocrmypdf", "--skip-text", "--optimize", "0",
           "--jobs", str(max(1, (os.cpu_count() or 4) - 2))]
    # Tesseract wants about 300 dpi; upscale a coarser scan before recognition.
    if dpi and dpi < 300:
        cmd += ["--oversample", "300"]
    cmd += [src, dst]
    subprocess.run(cmd, check=True)


def lettering(page):
    """How the lettering read on a page runs, as the page is shown.

    Returns the characters on lines running down the sheet, up it and level, and
    how many separate columns the sideways lines stand in.
    """
    down = up = level = 0
    columns = []
    turn = fitz.Matrix(page.rotation)    # PyMuPDF reports text as if /Rotate were 0
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", ()):
            count = sum(len(span["text"].replace(" ", "")) for span in line["spans"])
            slope = (fitz.Point(line["dir"]) * turn).y
            if abs(slope) <= 0.7:
                level += count
                continue
            if slope > 0:
                down += count
            else:
                up += count
            box = fitz.Rect(line["bbox"]) * page.rotation_matrix
            across = (box.x0 + box.x1) / 2
            if count and all(abs(across - other) > COLUMN_GAP for other in columns):
                columns.append(across)
    return down, up, level, len(columns)


def sideways_turn(page):
    """Quarter turn, clockwise as /Rotate counts it, that sets a page's lettering level; 0 if it is."""
    down, up, level, columns = lettering(page)
    if (columns < SIDEWAYS_COLUMNS or down + up < SIDEWAYS_CHARS
            or down + up < SIDEWAYS_SHARE * (down + up + level)):
        return 0
    return 270 if down >= up else 90


def long_words(page):
    return {word[4] for word in page.get_text("words") if len(word[4]) >= 3}


def read_turned(src, turns, dst, dpi):
    """OCR some pages of src into dst, each shown at its own /Rotate; returns them in dst's order."""
    numbers = sorted(turns)
    with pikepdf.open(src) as pdf, pikepdf.new() as part:
        for number in numbers:
            part.pages.append(pdf.pages[number])
            part.pages[-1].Rotate = turns[number] % 360
        part.save(dst + ".turned.pdf")
    run_ocr(dst + ".turned.pdf", dst, dpi)
    return numbers


def reread_sideways(src, ocred, dpi, keep_sideways=False):
    """Read sideways pages a second time turned level, and leave them level with that text.

    ocrmypdf squeezes a word that runs down the sheet into a square one letter
    tall, so nothing dragged over the lettering selects it, and Tesseract misreads
    more of a turned line than of a level one. Lettering that runs up the sheet it
    does not read at all. The page then stays level: Sioyek orders the letters
    of a line by how far across the page they stand, so it cannot select lettering
    that runs up or down however well the text is placed. With keep_sideways the
    page is turned back and looks as it did, for a reader that can.

    A page shown differently is a change a reader sees, so it takes three things.
    Sideways lettering in more than one column: a single column is the axis label
    of a level figure. The words read sideways found again level: a sideways
    reading says which way the lettering runs, but not reliably, and an
    upside-down reading finds none of them. And several times more level lettering
    than the page had as it stood. Returns the page numbers read again.
    """
    wanted, silent = {}, []
    with fitz.open(src) as before, fitz.open(ocred) as after:
        # ocrmypdf leaves a page that has its own text alone, and so does this.
        rotation = {number: before[number].rotation for number in range(len(after))
                    if not before[number].get_text().strip()}
        stood_level = {number: lettering(after[number])[2] for number in rotation}
        for number in rotation:
            turn, words = sideways_turn(after[number]), long_words(after[number])
            if turn and words:
                wanted[number] = (turn, words)
            elif sum(map(len, words)) < SIDEWAYS_CHARS:
                silent.append(number)
    kept = {}
    with tempfile.TemporaryDirectory(prefix="pdf-prep-turn-", dir=os.path.dirname(ocred)) as staging:
        if silent:
            # Lettering that runs up the sheet reads as nothing, so a page that
            # read next to nothing gets one more look turned half round.
            half = os.path.join(staging, "half.pdf")
            numbers = read_turned(src, {number: rotation[number] + 180 for number in silent}, half, dpi)
            with fitz.open(half) as document:
                for index, number in enumerate(numbers):
                    turn, words = sideways_turn(document[index]), long_words(document[index])
                    if turn and words:
                        wanted[number] = ((turn + 180) % 360, words)
        for half_turn in (0, 180):
            turns = {number: rotation[number] + wanted[number][0] + half_turn
                     for number in wanted if number not in kept}
            if not turns:
                break
            level = os.path.join(staging, "level-%d.pdf" % half_turn)
            numbers = read_turned(src, turns, level, dpi)
            with fitz.open(level) as document:
                for index, number in enumerate(numbers):
                    turn, words = wanted[number]
                    if (len(words & long_words(document[index])) >= SAME_WORDS * len(words)
                            and lettering(document[index])[2] >= LEVEL_GAIN * max(stood_level[number], 1)):
                        back = turn + half_turn if keep_sideways else 0
                        kept[number] = (level, index, (document[index].rotation - back) % 360)
        if kept:
            # The readings stay open until the save: their pages are copied then.
            readings = {read: pikepdf.open(read) for read, _, _ in kept.values()}
            try:
                with pikepdf.open(ocred, allow_overwriting_input=True) as pdf:
                    for number, (read, index, shown) in kept.items():
                        # Take the new reading into the page object already there: an
                        # outline entry or link that points at this page keeps working.
                        pdf.pages.append(readings[read].pages[index])
                        page, fresh = pdf.pages[number].obj, pdf.pages[-1].obj
                        for key in PAGE_DRAWING_KEYS:
                            if key in fresh:
                                page[key] = fresh[key]
                            elif key in page:
                                del page[key]
                        page.Rotate = shown
                        del pdf.pages[-1]
                    pdf.save(ocred)
            finally:
                for reading in readings.values():
                    reading.close()
    return sorted(kept)


# --------------------------------------------------------------------- verify

def verify_page(arguments):
    path, number = arguments
    with fitz.open(path) as document:
        page = document[number]
        fitz.TOOLS.mupdf_warnings(reset=True)
        pixmap = page.get_pixmap(dpi=36)
        warnings = fitz.TOOLS.mupdf_warnings(reset=True)
        if warnings:
            raise ValueError("page %d render warning: %s" % (number + 1, warnings))
        pixels = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
            pixmap.height, pixmap.width, pixmap.n)[..., :3]
        paper = [float(np.percentile(pixels[..., channel], 98)) for channel in range(3)]
        return number + 1, min(paper) < 240, len(page.get_text())


def verify(path, workers=1):
    """Measure the result -- a rendered page can look lighter than it truly is.

    Per channel, because a greyscale check passes a page whose paper has gone
    yellow: red hits 255 while blue lags far behind.
    """
    doc = fitz.open(path)
    if workers > 1:
        pages = len(doc)
        doc.close()
        tinted, characters = [], 0
        with ProcessPoolExecutor(max_workers=workers) as executor:
            arguments = ((path, number) for number in range(pages))
            for number, is_tinted, count in executor.map(verify_page, arguments, chunksize=4):
                if is_tinted:
                    tinted.append(number)
                characters += count
        return pages, tinted, characters
    tinted = []
    for pno in range(len(doc)):
        pix = doc[pno].get_pixmap(dpi=36)
        a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n)[..., :3]
        paper = [float(np.percentile(a[..., c], 98)) for c in range(3)]
        if min(paper) < 240:
            tinted.append(pno + 1)
    npages = len(doc)
    chars = sum(len(doc[p].get_text()) for p in range(npages))
    doc.close()
    return npages, tinted, chars


def open_reader(path, cache_pages=32, prerender_pages=3, page=None):
    """Open unchanged pixels in a fresh Sioyek instance with private preferences."""
    if sys.platform != "linux":
        raise ValueError("--read currently supports Linux Sioyek only")
    viewer = shutil.which("sioyek")
    if viewer is None:
        raise ValueError("--read requires sioyek on PATH")
    config_home = os.path.expanduser(os.environ.get("XDG_CONFIG_HOME", "~/.config"))
    user_config = os.path.join(config_home, "sioyek")
    with tempfile.TemporaryDirectory(prefix="pdf-prep-reader-") as profile:
        config = os.path.join(profile, "sioyek")
        os.makedirs(config)
        preferences = os.path.join(user_config, "prefs_user.config")
        lines = []
        if os.path.isfile(preferences):
            if "\n" in preferences or "\r" in preferences:
                raise ValueError("Sioyek preference paths cannot contain newlines")
            lines.append("source " + preferences)
        lines.extend(["num_cached_pages %d" % cache_pages,
                      "prerendered_page_count %d" % prerender_pages])
        with open(os.path.join(config, "prefs_user.config"), "w", encoding="utf-8") as destination:
            destination.write("\n".join(lines) + "\n")
        keys = os.path.join(user_config, "keys_user.config")
        if os.path.isfile(keys):
            shutil.copyfile(keys, os.path.join(config, "keys_user.config"))
        environment = os.environ.copy()
        environment["XDG_CONFIG_HOME"] = profile
        instance = os.path.basename(profile) + "-ipc"
        command = [viewer, "--new-instance", "--instance-name", instance]
        if page is not None:
            command.extend(["--page", str(page)])
        command.append(os.path.abspath(path))
        print("reader: %d cached pages, %d pre-rendered ahead; PDF and global preferences unchanged"
              % (cache_pages, prerender_pages), flush=True)
        print("reader: private Sioyek instance " + instance, flush=True)
        subprocess.run(command, env=environment, check=True)


def main():
    ap = argparse.ArgumentParser(
        prog="pdf-prep",
        description="Whiten the paper, speed up scrolling and OCR a scanned PDF, "
                    "doing only the parts the file actually needs.")
    ap.add_argument("input")
    ap.add_argument("output", nargs="?",
                    help="default: <input>-prep.pdf beside the source")
    ap.add_argument("--in-place", action="store_true", help="overwrite the input")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be done, writing nothing")
    ap.add_argument("--read", action="store_true",
                    help="open unchanged PDF in an isolated Linux Sioyek cache/pre-render profile; "
                         "do not process the PDF or change global viewer preferences")
    ap.add_argument("--reader-cache-pages", type=int,
                    help="rendered pages retained with --read (default: 32; range: 1-100)")
    ap.add_argument("--reader-prerender-pages", type=int,
                    help="pages prepared ahead with --read (default: 3; range: 0-10)")
    ap.add_argument("--reader-page", type=int, help="optional 1-based starting PDF page with --read")
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--no-turn", action="store_true",
                    help="leave a sideways page sideways; its text is placed, but Sioyek cannot select it")
    ap.add_argument("--no-whiten", action="store_true")
    ap.add_argument("--no-fast", action="store_true")
    ap.add_argument("--lossy-fast", action="store_true",
                    help="opt in to legacy global downsampling; can destroy figure detail")
    ap.add_argument("--black-text-fast", action="store_true",
                    help="opt in to black scanned text with original bitonal masks and native figure pixels; "
                         "requires --protect-regions; combine with --clip-figures to whiten and accelerate "
                         "the original scan in one pass; can enlarge the PDF")
    ap.add_argument("--compact-reading", action="store_true",
                    help="use tiny black colour fields on text-only pages and retain original compressed "
                         "figure images; requires --protect-regions; combine with --clip-figures and "
                         "--clean-edges; figure pages keep their original decoding cost")
    ap.add_argument("--crop-figures", action="store_true",
                    help="opt in to native raster transcoding; can enlarge scans by orders of magnitude; "
                         "requires --protect-regions")
    ap.add_argument("--clip-figures", action="store_true",
                    help="whiten outside protected figures using PDF clipping; retain compressed images "
                         "and masks unless a black-text reading mode is selected; requires --protect-regions")
    ap.add_argument("--clean-edges", action="store_true",
                    help="hide isolated outer-edge scan debris with small white vector overlays; "
                         "protect text and figure regions; requires --protect-regions and a separate output; "
                         "runs alone or after --clip-figures / either black-text reading mode")
    ap.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1),
                    help="raster-processing / clipping-verification worker processes (default: up to 4)")
    ap.add_argument("--no-compress", action="store_true",
                    help="skip the lossless size-reduction pass")
    ap.add_argument("--compress-only", action="store_true",
                    help="only losslessly compress existing layers; no whitening, resizing or OCR")
    ap.add_argument("--force-ocr", action="store_true",
                    help="OCR even if the file already has a text layer")
    ap.add_argument("--force-whiten", action="store_true",
                    help="run the whiten pass even if the survey sees little to do; "
                         "harmless, since already-white pages are skipped")
    ap.add_argument("--protect-regions", metavar="JSON",
                    help="preserve additional figure regions from an offline layout map")
    ap.add_argument("--text-only-pages", metavar="PAGES",
                    help="override false image detections on known text-only PDF pages, e.g. 53,60-62; "
                         "requires --protect-regions; overrides both layout and paper-picture detection")
    ap.add_argument("--no-migrate", action="store_true",
                    help="do not move Sioyek's highlights onto the new file")
    args = ap.parse_args()
    black_text_mode = args.black_text_fast or args.compact_reading
    black_text_name = "compact-reading" if args.compact_reading else "black-text-fast"
    reader_options = (args.reader_cache_pages, args.reader_prerender_pages, args.reader_page)
    if not args.read and any(value is not None for value in reader_options):
        ap.error("--reader-* options require --read")
    if args.read:
        if (args.output or args.in_place or args.lossy_fast or black_text_mode or args.crop_figures or args.clip_figures
                or args.clean_edges
                or args.compress_only or args.force_ocr or args.force_whiten or args.protect_regions
                or args.text_only_pages is not None
                or args.no_ocr or args.no_whiten or args.no_fast or args.no_compress):
            ap.error("--read is viewer-only and cannot be combined with output or PDF-processing options")
        cache_pages = args.reader_cache_pages if args.reader_cache_pages is not None else 32
        prerender_pages = args.reader_prerender_pages if args.reader_prerender_pages is not None else 3
        if not 1 <= cache_pages <= 100 or not 0 <= prerender_pages <= 10:
            ap.error("reader cache must be 1-100 pages; pre-rendering must be 0-10 pages")
        if cache_pages < prerender_pages + 2:
            ap.error("reader cache must hold at least the pre-rendered pages plus two visible pages")
        if not os.path.isfile(args.input):
            sys.exit("pdf-prep: cannot read " + args.input)
        if args.reader_page is not None:
            with fitz.open(args.input) as document:
                if not 1 <= args.reader_page <= len(document):
                    ap.error("--reader-page must be within this PDF")
        if args.dry_run:
            print("reader: %d cached pages, %d pre-rendered ahead; private Sioyek instance"
                  % (cache_pages, prerender_pages))
            print("dry run: PDF unchanged, no settings written, no viewer launched")
            return
        try:
            open_reader(args.input, cache_pages, prerender_pages, args.reader_page)
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            sys.exit("pdf-prep: " + str(exc))
        return
    if not 1 <= args.jobs <= 32:
        ap.error("--jobs must be between 1 and 32")
    if args.compress_only and args.no_compress:
        ap.error("--compress-only and --no-compress cannot be combined")
    if args.compact_reading and args.black_text_fast:
        ap.error("--compact-reading and --black-text-fast are alternative reading modes")
    if args.text_only_pages is not None and (not args.protect_regions or args.compress_only):
        ap.error("--text-only-pages requires --protect-regions and cannot be combined with --compress-only")
    if args.clean_edges and (not args.protect_regions or args.in_place or args.lossy_fast or args.crop_figures
                            or args.compress_only or args.force_ocr or args.force_whiten):
        ap.error("--clean-edges requires --protect-regions and a separate output; "
                 "only --clip-figures and a black-text reading mode may be combined with this processing mode")
    if black_text_mode and (not args.protect_regions or args.lossy_fast or args.crop_figures
                            or args.compress_only or args.force_ocr
                            or args.force_whiten or args.no_fast or args.in_place):
        ap.error("--%s requires --protect-regions and a separate output; "
                 "only --clip-figures and --clean-edges may be combined with this processing mode" % black_text_name)
    if args.clip_figures and (not args.protect_regions or args.crop_figures or args.lossy_fast
                             or args.compress_only or args.no_whiten or args.force_ocr):
        ap.error("--clip-figures requires --protect-regions and cannot be combined with "
                 "--crop-figures, --lossy-fast, --compress-only, --force-ocr or --no-whiten")
    if args.crop_figures and (not args.protect_regions or args.lossy_fast
                             or args.no_fast or args.compress_only):
        ap.error("--crop-figures requires --protect-regions and native fast processing")
    if args.lossy_fast and (args.no_fast or args.compress_only):
        ap.error("--lossy-fast cannot be combined with --no-fast or --compress-only")

    if not os.path.isfile(args.input):
        sys.exit("pdf-prep: cannot read " + args.input)
    if args.output and args.in_place:
        sys.exit("pdf-prep: give an output path or --in-place, not both")

    stem, ext = os.path.splitext(args.input)
    suffix = "-" + black_text_name if black_text_mode else ("-clean-edges" if args.clean_edges else "-prep")
    dst = args.output or (args.input if args.in_place else stem + suffix + ext)
    if black_text_mode and os.path.realpath(args.input) == os.path.realpath(dst):
        ap.error("--%s requires an output different from the input" % black_text_name)
    if args.clean_edges and os.path.realpath(args.input) == os.path.realpath(dst):
        ap.error("--clean-edges requires an output different from the input")

    src_sum = md5_of(args.input)
    s = survey(args.input)
    regions = None
    text_only_pages = set()
    if args.protect_regions:
        try:
            regions, text_only_pages = load_regions(args.protect_regions, src_sum, s["pages"], include_overrides=True)
            if args.text_only_pages is not None:
                text_only_pages.update(page_numbers(args.text_only_pages, s["pages"]))
            for number in text_only_pages:
                regions[str(number)] = []
        except (OSError, ValueError, TypeError) as exc:
            sys.exit("pdf-prep: " + str(exc))
    kind = "Internet Archive (MRC layered)" if s["is_mrc"] else "single image per page"
    print("%d pages, %s, about %d dpi" % (s["pages"], kind, s["dpi"]))
    print("  text layer : %s (%d of %d pages)"
          % ("missing" if s["needs_ocr"] else "present", s["texted"], s["pages"]))
    print("  paper      : %s" % ("tinted" if s["needs_whiten"] else "already white"))
    if text_only_pages:
        print("  text-only  : PDF pages %s (manual overrides)" % sorted(text_only_pages))

    do_whiten = ((s["needs_whiten"] or args.force_whiten or args.clip_figures) and not args.no_whiten
                 and not args.compress_only and (not black_text_mode or args.clip_figures)
                 and (not args.clean_edges or args.clip_figures))
    do_fast = (black_text_mode or args.crop_figures or (args.lossy_fast and s["needs_fast"])) and not args.no_fast and not args.compress_only
    do_ocr = ((s["needs_ocr"] or args.force_ocr) and not args.no_ocr
              and not args.compress_only and not args.clip_figures and not black_text_mode
              and not args.clean_edges)
    do_compress = ((s["needs_compress"] or do_whiten or do_fast or do_ocr or args.compress_only)
                   and not args.no_compress and not (do_fast and not args.lossy_fast)
                   and not args.clip_figures and not black_text_mode and not args.clean_edges)
    fast_name = black_text_name if black_text_mode else ("lossy-fast" if args.lossy_fast else "native-crops")
    white_name = "compressed-clips" if args.clip_figures else "whiten"
    plan = [n for n, on in ((white_name, do_whiten), (fast_name, do_fast), ("clean-edges", args.clean_edges), ("ocr", do_ocr),
                            ("compress", do_compress)) if on]
    print("  plan       : %s" % (", ".join(plan) if plan else "nothing to do"))
    if args.crop_figures:
        print("  warning    : raster transcoding can make this PDF vastly larger")

    if args.dry_run:
        print("dry run: nothing written")
        return
    if not plan:
        print("nothing to do")
        return

    workdir = os.path.dirname(os.path.abspath(dst)) or "."
    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=workdir)
    os.close(fd)
    try:
        if black_text_mode:
            if args.clip_figures:
                with tempfile.TemporaryDirectory(prefix="pdf-prep-white-", dir=workdir) as staging:
                    white_path = os.path.join(staging, "white.pdf")
                    white, pictures, unchanged = whiten(args.input, white_path, regions, clip=True,
                                                       text_only_pages=text_only_pages)
                    print("compressed-clips: %d pages to white, %d around protected pictures, %d left alone"
                          % (white, pictures, unchanged), flush=True)
                    stats = prepare_black_text(white_path, tmp, regions, workers=args.jobs,
                                               keep_figure_encoding=args.compact_reading)
            else:
                stats = prepare_black_text(args.input, tmp, regions, workers=args.jobs,
                                           keep_figure_encoding=args.compact_reading)
            figure_kind = "original-figure" if args.compact_reading else "native-figure"
            print("%s: %d black-text pages, %d %s pages, %d unchanged"
                  % (black_text_name, stats["black_pages"], stats["figure_pages"], figure_kind,
                     stats["unchanged"]), flush=True)
        elif do_fast and not args.lossy_fast:
            stats = prepare_native(args.input, tmp, regions, clean=do_whiten,
                                   crop=args.crop_figures, workers=args.jobs, text_only_pages=text_only_pages)
            if args.crop_figures and not stats["foregrounds"]:
                sys.exit("pdf-prep: no native MRC foregrounds found; restart from the original scan")
            print("native: %d foregrounds cropped without resizing; %d white backgrounds, "
                  "%d with original picture pixels, %d unchanged" % (
                      stats["foregrounds"], stats["white"], stats["pictures"], stats["unchanged"]))
        elif do_whiten:
            w, c, a = whiten(args.input, tmp, regions, clip=args.clip_figures, text_only_pages=text_only_pages)
            print("%s: %d pages to white, %d around protected pictures, %d left alone"
                  % (white_name, w, c, a))
        else:
            shutil.copyfile(args.input, tmp)

        if do_fast and args.lossy_fast:
            print("lossy-fast: shrank %d colour images; figure detail may be lost"
                  % slim_foregrounds(tmp))

        if do_ocr:
            fd2, tmp2 = tempfile.mkstemp(suffix=".pdf", dir=workdir)
            os.close(fd2)
            try:
                run_ocr(tmp, tmp2, s["dpi"])
                # Sioyek places a note by how far down the whole document it is, so
                # a page turned level would move every note below it.
                notes = 0 if args.no_migrate else sioyek_notes(src_sum)
                turned = reread_sideways(tmp, tmp2, s["dpi"], keep_sideways=args.no_turn or notes > 0)
                if turned:
                    print("ocr: %d sideways pages %s: %s%s"
                          % (len(turned),
                             "read level and left sideways" if args.no_turn or notes else "now stand level",
                             [number + 1 for number in turned[:10]],
                             " ..." if len(turned) > 10 else ""), flush=True)
                    if notes and not args.no_turn:
                        print("  left sideways: Sioyek holds notes on this file (%d), which a turned page"
                              " would move; Sioyek cannot select sideways lettering" % notes, flush=True)
                os.replace(tmp2, tmp)
            except subprocess.CalledProcessError as exc:
                os.path.exists(tmp2) and os.unlink(tmp2)
                sys.exit("pdf-prep: ocrmypdf failed (%s)" % exc.returncode)

        if do_compress:
            stats = compact(tmp)
            print("compress: %d images, %d streams; %.2f -> %.2f MiB (lossless)"
                  % (stats["images"], stats["streams"], stats["before_bytes"] / 1024 ** 2,
                     stats["after_bytes"] / 1024 ** 2))

        if args.clean_edges:
            edge_fd, edge_path = tempfile.mkstemp(suffix=".pdf", dir=workdir)
            os.close(edge_fd)
            try:
                stats = clean_edges(tmp, edge_path, regions, workers=args.jobs)
                os.replace(edge_path, tmp)
            finally:
                if os.path.exists(edge_path):
                    os.unlink(edge_path)
            print("clean-edges: %d pages cleaned, %d small overlays, %d left unchanged"
                  % (stats["pages"], stats["rectangles"], stats["unchanged"]), flush=True)

        npages, tinted, chars = verify(tmp, workers=args.jobs if args.clip_figures or black_text_mode or args.clean_edges else 1)
        print("verify: all %d pages render; %d still tinted; %d characters of text"
              % (npages, len(tinted), chars))
        if tinted:
            print("  tinted pages: %s%s"
                  % (tinted[:10], " ..." if len(tinted) > 10 else ""))
        if md5_of(args.input) != src_sum:
            sys.exit("pdf-prep: the input changed during processing; output not published")
        if black_text_mode:
            preservation = "original figure encodings retained" if args.compact_reading else "protected picture pixels retained"
            print("size: %.2f -> %.2f MiB (%s)"
                  % (os.path.getsize(args.input) / 1024 ** 2, os.path.getsize(tmp) / 1024 ** 2,
                     preservation))
        shutil.move(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    if not args.no_migrate:
        moved = migrate_sioyek(src_sum, md5_of(dst))
        if moved:
            print("sioyek: moved %s onto the new file"
                  % ", ".join("%d %s" % (n, k.split(".")[0]) for k, n in moved.items()))
            print("  (close Sioyek before running this, or it may write back stale state)")

    print(dst)


if __name__ == "__main__":
    main()
