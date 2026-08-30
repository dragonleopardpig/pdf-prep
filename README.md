# pdf-prep

Make a scanned PDF pleasant to read: white paper, fast scrolling, selectable text.

It surveys the file first and does only what that scan actually needs, so it is safe
to point at anything and safe to run twice.

```console
$ pdf-prep --dry-run "Optical Alignment I.pdf"
260 pages, Internet Archive (MRC layered), about 359 dpi
  text layer : present (256 of 260 pages)
  paper      : tinted
  plan       : whiten, fast
```

## Why this exists

Internet Archive scans are **MRC-layered**: each page is a low-resolution RGB
*background* holding the paper and any photograph, with the ink painted over it through
a high-resolution **JBIG2 stencil mask**.

That structure is the whole point. It means you can rewrite the paper *without touching
the text*, because the text lives in the mask. Ordinary "clean up a scan" tools
re-render the page and level-adjust it, which softens the type and throws away the MRC
compression. This doesn't.

It also explains why these files scroll so badly. The foreground is a ~10-megapixel
JPEG 2000 that supplies **nothing but ink colour** — the mask carries the shapes. PDF
allows a soft mask to differ in pixel size from the image it masks, so that colour
field can shrink to a thumbnail while the text stays exactly as sharp.

Measured on one 328-page book: **596 ms/page → 120 ms/page**, 10% smaller, rendering
indistinguishable.

## What it does

| pass | when | what |
|---|---|---|
| **whiten** | paper is tinted | rewrites the background layer only; text mask untouched |
| **fast** | an oversized MRC colour layer is present | shrinks it to 128 px; ~5× faster to render |
| **ocr** | no real text layer | `ocrmypdf`, with `--oversample` chosen from the scan's dpi |

Not every scan needs every pass. A DuXiu scan (one bitonal image per page) is already
white and already fast — whitening one would only smear JPEG ringing around razor-sharp
type, so the tool refuses.

## Install

Needs Python 3.9+ with PyMuPDF, NumPy, Pillow and pikepdf, plus `ocrmypdf` and
`tesseract` on `PATH` if you want the OCR pass.

```console
pip install .
```

On NixOS, the flake exposes it directly:

```nix
inputs.pdf-prep.url = "github:dragonleopardpig/pdf-prep";
# then: inputs.pdf-prep.packages.${system}.default
```

## Usage

```console
pdf-prep --dry-run in.pdf       # report the plan, write nothing
pdf-prep in.pdf                 # writes in-prep.pdf beside the source
pdf-prep --in-place in.pdf      # overwrite
```

`--no-ocr`, `--no-whiten`, `--no-fast` skip a pass; `--force-ocr` and `--force-whiten`
run one the survey did not plan. `--no-verify` skips the measurement pass at the end.

### Sioyek highlights

Sioyek identifies a document by the MD5 of its whole contents, so **any** edit orphans
its highlights even though the pages are unchanged. Nothing here alters page geometry —
only the images inside each page — so the stored coordinates stay valid and just need
re-keying. If a Sioyek database is present, `pdf-prep` moves highlights, bookmarks,
marks and links onto the rewritten file automatically. `--no-migrate` opts out;
`PDF_PREP_SIOYEK_DB` points it elsewhere.

Close Sioyek before rewriting a file it has open, or it may write stale state back.

## How it decides

Two questions have to be answered per page, and both are harder than they look.

**Is there a picture in this layer?** Not answerable by any absolute tone statistic.
Global standard deviation cannot tell a photograph from an uneven scanner lamp — a page
merely brighter at the top scores the same as one with a photo on it. High-frequency
energy is better but does not port between scans: one book's blank pages score 0.4 and
another's 2.0.

So it uses two scale-free measures — how much of the layer is *darker than the paper*
(in an MRC background the text is in the mask, so anything dark **is** a picture), and
how *localised* the fine detail is (a photograph is concentrated; scanner noise is
spread evenly) — and calibrates both against each document's own median, since most
pages of a scanned book carry no picture at all.

**Where is the paper, and what colour is it?** Per channel, over one shared set of
paper pixels chosen by luminance. Letting each channel pick its own reference makes
them latch onto different features on a photo-heavy page — paper in one, a photo
highlight in another — which tints whole pages purple. And the white point is taken at
the *lower edge* of the paper's distribution, not a high percentile, or only the
brightest tail reaches white and the bulk stays visibly grey.

Colour covers are detected by 95th-percentile saturation, never the mean: a cover is
mostly pale artwork around one saturated block, so its *mean* saturation looks like a
text page and the cover gets blown out.

## Limitations

- **The picture/paper thresholds are validated, not proven.** Measured across 52
  MRC-structured PDFs (816 sampled pages), per-document calibration gives a median
  relative margin of 0.37 and a worst case of 0.012. That is a thin worst case. Pages
  near the boundary can be misclassified, which shows up as a page keeping a faint
  gradient rather than going white. `--dry-run` reports what it plans.
- Only Internet Archive-style MRC scans benefit from `whiten` and `fast`. Everything
  else is surveyed and left alone.
- Files usually grow, because pages without an MRC split get re-encoded out of
  JPEG 2000.
- Colour covers and endpapers are deliberately preserved, so the verify pass will
  always report a few pages as still tinted. That is correct.

## Verifying its work

After writing, it re-opens the result, renders every page and reports how many still
have tinted paper. Measure per channel if you check this yourself: a greyscale test
passes a page whose paper has gone *yellow*, because red is already at 255.

`MuPDF error: cannot find object in xref` during a save is noise, not damage — one per
replaced image object while garbage collection runs. It is silenced.

## Licence

MIT.
