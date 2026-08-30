"""Make a scanned PDF pleasant to read: white paper, fast scrolling, selectable text.

Works out what the file actually needs rather than applying everything blindly.

Internet Archive scans layer each page as a low-resolution RGB background (the paper,
plus any continuous-tone photo) with the ink painted over it through a high-resolution
JBIG2 stencil mask. That structure is why they need whitening (their paper can sit at
60% grey) and why they scroll badly (a 10 megapixel JPEG 2000 decoded per page just to
supply ink colour). DuXiu scans are a single bitonal image per page: already white,
already fast, and usually just missing OCR.
"""
import argparse
import hashlib
import io
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
from PIL import Image

PAPER_FLOOR = 40     # a channel's paper level cannot sensibly fall below this
DARKFRAC_FLOOR = 0.0015   # below this a page is paper however busy the book
DARKFRAC_CEIL = 0.05      # above this it is a picture however busy the book
TILERATIO_FLOOR = 2.5
TILERATIO_CEIL = 15.0
DARKFRAC_CEIL_FALLBACK = 0.003    # used when a document is too short to calibrate
TILERATIO_CEIL_FALLBACK = 6.0
MIN_BG_DIM = 1500    # a "background" wider than this is really a full-page image
ALREADY_WHITE = 245  # paper at least this bright is done; touching it only degrades
TEXT_PER_PAGE = 50   # a page with fewer characters than this counts as untexted
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


def migrate_sioyek(old_sum, new_sum):
    """Re-point Sioyek's annotations at the rewritten file.

    Sioyek identifies a document by the MD5 of its entire contents, so *any* edit
    orphans its highlights, bookmarks and marks even though the pages are
    unchanged. Nothing here alters page geometry -- only the images inside each
    page -- so the stored coordinates stay valid and just need re-keying.
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


# ---------------------------------------------------------------- tone helpers

def paper_levels(rgb):
    """Per-channel paper level, measured over ONE shared set of paper pixels.

    The level is taken at the lower edge of the paper's distribution, not at a high
    percentile: a percentile lifts only the brightest tail and leaves the bulk of the
    paper visibly grey.

    Crucially the paper pixels are chosen once, by luminance, and every channel is
    then read over that same set. Letting each channel find its own mode lets them
    latch onto different features on a photo-heavy page -- paper in one channel, a
    photo highlight in another -- which produced gains like (1.67, 1.40, 1.76) and
    tinted whole pages purple.
    """
    grey = rgb.mean(axis=2)
    lo = PAPER_FLOOR
    hist = np.bincount(grey.astype(np.uint8).ravel(), minlength=256)
    band = hist[lo:]
    if band.sum() == 0:
        return [float(max(np.percentile(rgb[..., c], 99), lo)) for c in range(3)]
    mode = int(np.argmax(band)) + lo
    sel = (grey >= mode - 15) & (grey <= mode + 15)
    if sel.sum() < 0.02 * grey.size:      # too few: widen rather than guess
        sel = grey >= mode - 30
    out = []
    for c in range(3):
        v = rgb[..., c][sel].astype(np.float32)
        out.append(float(max(lo, v.mean() - 3.0 * max(v.std(), 2.0))))
    return out


def background_image(imgs):
    """The paper layer: the image with no soft mask.

    Not simply the smallest image. In an MRC page the foreground carries the JBIG2
    stencil as its /SMask and the background does not, and once --fast has shrunk
    that foreground to a thumbnail it becomes the smallest image on the page. A
    size-based rule would then pick the ink-colour layer and whiten *that*,
    wrecking the page on a second run.
    """
    plain = [i for i in imgs if i[1] == 0]
    if not plain:
        return None
    return min(plain, key=lambda i: i[2] * i[3])


def page_metrics(rgb):
    """The two scale-free measures used to spot a picture in the paper layer.

    darkfrac -- share of the layer notably darker than the paper. In an MRC
    background the text lives in the mask, so anything dark IS a picture.
    tileratio -- how localised the fine detail is. A picture is concentrated;
    scanner noise is spread evenly.
    """
    grey = rgb.mean(axis=2)
    hist = np.bincount(grey.astype(np.uint8).ravel(), minlength=256)
    mode = int(np.argmax(hist[120:])) + 120
    darkfrac = float((grey < mode - 45).mean())

    im = Image.fromarray(grey.astype(np.uint8), "L")
    small = im.resize((max(1, im.width // 16), max(1, im.height // 16)), Image.BILINEAR)
    blur = np.asarray(small.resize(im.size, Image.BILINEAR), dtype=np.float32)
    resid = grey - blur
    h, w = resid.shape
    th, tw = max(1, h // 12), max(1, w // 12)
    e = np.array([resid[i:i + th, j:j + tw].std()
                  for i in range(0, h - th + 1, th)
                  for j in range(0, w - tw + 1, tw)])
    med = float(np.median(e))
    if med < 0.05:
        # Already flat everywhere; a ratio here would divide by ~zero.
        tileratio = float(e.max()) / 2.0
    else:
        tileratio = float(np.percentile(e, 95) / med)
    return darkfrac, tileratio


def calibrate(doc, sample=25):
    """Learn this document's own paper baseline.

    Fixed thresholds do not port between scans. Measured over 52 MRC documents,
    the paper side of darkfrac reached 0.0030 against a 0.003 cut -- no margin at
    all -- and 44% of pages sat near a boundary. Most pages of a scanned book
    carry no picture, so that book's own median is its paper level and a picture
    is an outlier against it. Calibrating per document lifted the median relative
    margin from 0.23 to 0.37 on the same corpus.

    The floors stop a book with a picture on every page from calling everything
    paper; the ceilings stop a very clean book from calling faint texture a
    picture.
    """
    values = []
    step = max(1, len(doc) // sample)
    for pno in range(0, len(doc), step):
        imgs = doc[pno].get_images(full=True)
        small = background_image(imgs)
        if small is None or small[2] >= MIN_BG_DIM:
            continue
        try:
            pix = fitz.Pixmap(doc.extract_image(small[0])["image"])
            if pix.n > 3:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n)
        except Exception:
            continue
        if a.shape[2] == 1:
            a = np.repeat(a, 3, axis=2)
        values.append(page_metrics(a[..., :3]))

    if len(values) < 6:
        return DARKFRAC_CEIL_FALLBACK, TILERATIO_CEIL_FALLBACK
    array = np.array(values)
    out = []
    for column, floor, ceil in ((0, DARKFRAC_FLOOR, DARKFRAC_CEIL),
                                (1, TILERATIO_FLOOR, TILERATIO_CEIL)):
        v = array[:, column]
        median = float(np.median(v))
        deviation = float(np.median(np.abs(v - median))) or 1e-6
        out.append(float(np.clip(median + 8.0 * deviation, floor, ceil)))
    return out[0], out[1]


def has_picture(rgb, thresholds=None):
    """Is there a real picture in this layer, or only paper?"""
    darkfrac_t, tileratio_t = thresholds or (DARKFRAC_CEIL_FALLBACK,
                                             TILERATIO_CEIL_FALLBACK)
    darkfrac, tileratio = page_metrics(rgb)
    return darkfrac > darkfrac_t or tileratio > tileratio_t

def gain_lut(white_point):
    """Linear white balance -- what the scanner should have done."""
    lut = np.arange(256, dtype=np.float32) * (255.0 / max(white_point, 1.0))
    return np.clip(lut, 0, 255).astype(np.uint8)


def balance_and_deepen(rgb, levels, gamma=1.8):
    """White-balance a layer that holds both ink and paper, then restore ink density.

    A fixed luminance knee cannot be used here. On strongly yellowed paper the blue
    channel sits *below* the knee, so red and green get lifted to white while blue is
    left untouched -- turning a mild yellow into a vivid one (measured: paper coming
    out rgb(254,254,97)).

    So balance each channel against its own paper level first, which makes the paper
    neutral white whatever its tint. That lifts the ink too, so a gamma then pulls the
    midtones back down: white stays white, and the text returns to roughly its
    original density instead of washing out to grey.
    """
    out = np.empty_like(rgb)
    for c in range(3):
        out[..., c] = gain_lut(levels[c])[rgb[..., c]]
    lut = (255.0 * (np.arange(256, dtype=np.float32) / 255.0) ** gamma)
    return np.clip(lut, 0, 255).astype(np.uint8)[out]


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
        if not imgs:
            continue
        if len(imgs) >= 2 and min(i[2] for i in imgs) < MIN_BG_DIM:
            mrc += 1
        # get_images tuples are (xref, smask, width, height, ...): a wide image
        # carrying a soft mask is the costly JPEG 2000 colour layer. If it has
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
    }


# ------------------------------------------------------------------ whitening

def whiten(path, dst):
    doc = fitz.open(path)
    thresholds = calibrate(doc)
    white = curved = alone = 0
    for pno in range(len(doc)):
        page = doc[pno]
        imgs = page.get_images(full=True)
        if not imgs:
            continue
        small = background_image(imgs)
        if small is None:
            alone += 1
            continue
        xref, w, h = small[0], small[2], small[3]
        try:
            raw = doc.extract_image(xref)
            pix = fitz.Pixmap(raw["image"])
            if pix.n > 3:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n)
        except Exception:
            alone += 1
            continue

        # A bitonal or greyscale scan arrives with a single channel; widen it so
        # everything below can assume three, and save it back as grey.
        is_grey = a.shape[2] == 1
        rgb = np.repeat(a, 3, axis=2) if is_grey else a[..., :3]
        grey = rgb.mean(axis=2)

        if w > MIN_BG_DIM:
            # No MRC split: ink and paper share one image. Judge colour by a high
            # percentile of saturation, not its mean -- a cover is mostly pale
            # artwork around one saturated block, which drags the mean down to
            # text-page levels. Measured, covers sit at 112-176 and text pages at
            # 0-23. No upper bound on p99: an already-light page still needs the
            # last push.
            p99 = float(np.percentile(grey, 99))
            sat_p95 = float(np.percentile(rgb.max(axis=2) - rgb.min(axis=2), 95))
            if not (grey.mean() > 120 and p99 >= 130 and sat_p95 < 60):
                alone += 1
                continue
            levels = paper_levels(rgb)
            if min(levels) >= ALREADY_WHITE:
                alone += 1
                continue
            out = balance_and_deepen(rgb, levels)
            buf = io.BytesIO()
            if is_grey:
                Image.fromarray(out[..., 0], "L").save(buf, "JPEG", quality=85)
            else:
                Image.fromarray(out, "RGB").save(buf, "JPEG", quality=85)
            page.replace_image(xref, stream=buf.getvalue())
            curved += 1
            continue

        if not has_picture(rgb, thresholds):
            # Paper only, however unevenly it was lit: flatten it outright. A gain
            # would keep the shading, and any colour in it, which is what left
            # tinted bands along the bottom of these pages.
            buf = io.BytesIO()
            Image.new("L", (w, h), 255).save(buf, "PNG")
            page.replace_image(xref, stream=buf.getvalue())
            white += 1
        else:
            # Balance each channel separately. Scanned paper is warm, so one
            # luminance curve drives red to 255 while blue lags, turning the paper
            # yellow instead of white.
            levels = paper_levels(rgb)
            if min(levels) >= ALREADY_WHITE:
                alone += 1
                continue
            out = np.empty_like(rgb)
            for c in range(3):
                out[..., c] = gain_lut(levels[c])[rgb[..., c]]
            buf = io.BytesIO()
            if is_grey:
                Image.fromarray(out[..., 0], "L").save(buf, "JPEG", quality=88)
            else:
                Image.fromarray(out, "RGB").save(buf, "JPEG", quality=88, subsampling=0)
            page.replace_image(xref, stream=buf.getvalue())
            curved += 1

    fitz.TOOLS.mupdf_display_errors(False)
    doc.save(dst, garbage=4, deflate=True)
    fitz.TOOLS.mupdf_display_errors(True)
    doc.close()
    return white, curved, alone


# ----------------------------------------------------------------- speed pass

def slim_foregrounds(path, max_side=128):
    """Shrink the MRC foreground, which is only a smooth field of ink colour.

    Decoding ten megapixels of JPEG 2000 per page is what makes these files crawl.
    The stencil carries the letter shapes, and PDF lets a soft mask have different
    pixel dimensions from the image it masks, so the colour field can shrink to a
    thumbnail while the text stays exactly as sharp.

    Must be pikepdf: PyMuPDF's replace_image drops /SMask, losing the stencil and
    painting each page a solid black smear.
    """
    pdf = pikepdf.open(path, allow_overwriting_input=True)
    slimmed = 0
    for page in pdf.pages:
        xobjs = page.get("/Resources", {}).get("/XObject", {})
        for _, obj in dict(xobjs).items():
            if obj.get("/Subtype") != "/Image" or "/SMask" not in obj:
                continue
            w, h = int(obj.Width), int(obj.Height)
            if w < MIN_BG_DIM or max(w, h) <= max_side:
                continue
            try:
                im = Image.open(io.BytesIO(obj.read_raw_bytes())).convert("RGB")
            except Exception:
                continue
            scale = max_side / float(max(w, h))
            small = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                              Image.BILINEAR)
            obj.write(zlib.compress(np.asarray(small, dtype=np.uint8).tobytes()),
                      filter=pikepdf.Name("/FlateDecode"))
            obj.Width, obj.Height = small.width, small.height
            obj.ColorSpace = pikepdf.Name("/DeviceRGB")
            obj.BitsPerComponent = 8
            for k in ("/DecodeParms", "/Decode"):
                if k in obj:
                    del obj[k]
            slimmed += 1
    pdf.save(path)
    return slimmed


# ------------------------------------------------------------------------ OCR

def run_ocr(src, dst, dpi):
    cmd = ["ocrmypdf", "--skip-text", "--optimize", "0",
           "--jobs", str(max(1, (os.cpu_count() or 4) - 2))]
    # Tesseract wants about 300 dpi; upscale a coarser scan before recognition.
    if dpi and dpi < 300:
        cmd += ["--oversample", "300"]
    cmd += [src, dst]
    subprocess.run(cmd, check=True)


# --------------------------------------------------------------------- verify

def verify(path):
    """Measure the result -- a rendered page can look lighter than it truly is.

    Per channel, because a greyscale check passes a page whose paper has gone
    yellow: red hits 255 while blue lags far behind.
    """
    doc = fitz.open(path)
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
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--no-whiten", action="store_true")
    ap.add_argument("--no-fast", action="store_true")
    ap.add_argument("--force-ocr", action="store_true",
                    help="OCR even if the file already has a text layer")
    ap.add_argument("--force-whiten", action="store_true",
                    help="run the whiten pass even if the survey sees little to do; "
                         "harmless, since already-white pages are skipped")
    ap.add_argument("--no-migrate", action="store_true",
                    help="do not move Sioyek's highlights onto the new file")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        sys.exit("pdf-prep: cannot read " + args.input)
    if args.output and args.in_place:
        sys.exit("pdf-prep: give an output path or --in-place, not both")

    stem, ext = os.path.splitext(args.input)
    dst = args.output or (args.input if args.in_place else stem + "-prep" + ext)

    src_sum = md5_of(args.input)
    s = survey(args.input)
    kind = "Internet Archive (MRC layered)" if s["is_mrc"] else "single image per page"
    print("%d pages, %s, about %d dpi" % (s["pages"], kind, s["dpi"]))
    print("  text layer : %s (%d of %d pages)"
          % ("missing" if s["needs_ocr"] else "present", s["texted"], s["pages"]))
    print("  paper      : %s" % ("tinted" if s["needs_whiten"] else "already white"))

    do_whiten = (s["needs_whiten"] or args.force_whiten) and not args.no_whiten
    do_fast = s["needs_fast"] and not args.no_fast
    do_ocr = (s["needs_ocr"] or args.force_ocr) and not args.no_ocr
    plan = [n for n, on in (("whiten", do_whiten), ("fast", do_fast), ("ocr", do_ocr)) if on]
    print("  plan       : %s" % (", ".join(plan) if plan else "nothing to do"))

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
        if do_whiten:
            w, c, a = whiten(args.input, tmp)
            print("whiten: %d pages to white, %d balanced, %d left alone" % (w, c, a))
        else:
            shutil.copyfile(args.input, tmp)

        if do_fast:
            print("fast: shrank the colour layer on %d pages (text untouched)"
                  % slim_foregrounds(tmp))

        if do_ocr:
            # OCR last so it sees the whitened images, and --optimize 0 so it does
            # not re-encode the work above.
            fd2, tmp2 = tempfile.mkstemp(suffix=".pdf", dir=workdir)
            os.close(fd2)
            try:
                run_ocr(tmp, tmp2, s["dpi"])
                os.replace(tmp2, tmp)
            except subprocess.CalledProcessError as exc:
                os.path.exists(tmp2) and os.unlink(tmp2)
                sys.exit("pdf-prep: ocrmypdf failed (%s)" % exc.returncode)

        npages, tinted, chars = verify(tmp)
        print("verify: all %d pages render; %d still tinted; %d characters of text"
              % (npages, len(tinted), chars))
        if tinted:
            print("  tinted pages: %s%s"
                  % (tinted[:10], " ..." if len(tinted) > 10 else ""))
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
