# pdf-prep

Make a scanned PDF pleasant to read: white paper, fast scrolling, selectable text.

It surveys the file first and does only what that scan actually needs. It writes
a separate copy by default; inspect figures before replacing an original scan.

## White paper and faster scrolling

For a smaller reading copy that keeps the original figure compression, use the
original PDF and its figure map:

```console
pdf-prep --clip-figures --compact-reading --clean-edges --protect-regions figures.json \
  --no-migrate original.pdf reading-compact.pdf
```

This explicitly accepts solid-black scanned text on pages without protected
figures. Those pages use tiny constant-black colour fields and retain their
original text stencils. Pages with figures retain the original compressed colour
image, resolution and pixels, including the original text colour on that page.
Selectable OCR remains intact. Whitening and edge cleanup use small vector
overlays. All stages use the original map, so an intermediate PDF and a second
checksum-bound map are unnecessary. Every output page is verified before the
result is published.

`--compact-reading` replaces only colour fields on explicitly mapped text-only
pages that pass the existing MRC safety checks. Shared images used by a figure
page are retained for every use. Unlisted and uncertain pages remain unchanged.
Repeating the reading stage leaves already-processed images unchanged. This mode
does not restore compressed originals from an earlier raster-transcoded copy;
restart from the original PDF. It cannot be combined with `--black-text-fast`,
lossy processing, OCR, in-place output or compression-only processing.

For faster decoding on figure pages as well, substitute `--black-text-fast` for
`--compact-reading`. That mode preserves native figure pixels but converts their
colour fields to lossless PNG-Flate, which can make the file much larger.

`--clip-figures` alone preserves the original foreground encoding and therefore
does not remove its rendering cost. The faster copy can be larger on disk; decoded
image complexity, rather than compressed file size, determines much of the delay.

### Alternative compression investigation

The 398 MB cleaned reading copy stores 363 MiB in its 356 lossless figure colour
fields. Increasing PNG compression to level 9 saved about 6% on sampled Figures
2-8/2-10. Lossless JPEG 2000 saved about 14–19%, but decoding full-page fields
remained expensive. Its lossless mode is documented by
[Pillow](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#jpeg-2000).
Removing colour under transparent stencil pixels reduced size further, but
changed some visible edges during downscaled rendering; that approach was
rejected. The compact reading mode instead keeps the original compressed figure
streams and accelerates the text-only pages.

### Optical Electronics: compact reading copy

`artifacts/OPTICAL ELECTRONICS FOURTH EDITION-reading-compact.pdf` contains all
744 pages at **31.4 MB (29.93 MiB)**, versus the original 46.9 MB and the earlier
398 MB cleaned fast copy. It uses 387 constant-black text colour fields, keeps
356 original compressed figure foregrounds and leaves the front cover unchanged.
All original background images and text stencils retain their encoded bytes.
733 pages receive conservative edge cleanup; the two covers keep their natural
paper colour. Figure pages retain their original text colour and decoding cost.

Rebuild it directly from the original with:

```console
pdf-prep --clip-figures --compact-reading --clean-edges \
  --protect-regions artifacts/contents-corrected-regions.json --no-migrate \
  "artifacts/OPTICAL ELECTRONICS FOURTH EDITION.pdf" \
  "artifacts/OPTICAL ELECTRONICS FOURTH EDITION-reading-compact.pdf"
```

`artifacts/reading-compact-validation.json` records the complete-book preservation
checks. The generated copy can be opened using the local Sioyek build:

```console
PATH="$PWD/../sioyek/build:$PATH" QT_QPA_PLATFORM=wayland \
  pdf-prep --read --reader-prerender-pages 6 \
  "artifacts/OPTICAL ELECTRONICS FOURTH EDITION-reading-compact.pdf"
```

If a short mouse drag selects a large unrelated text block, the PDF's OCR may
still be correct. On this desktop, Kvantum's window-drag manager intercepted
canvas drags and sent a synthetic mouse release at `(-1, -1)`, reproducing the
same selection error in the original and compact PDFs. The local Sioyek build
now excludes its document canvases from that behavior using
`_kde_no_window_grab`, supported by
[Kvantum's window manager](https://github.com/tsujan/Kvantum/blob/master/Kvantum/style/drag/windowmanager.cpp).
Restart the local viewer with the command above to use the fix; no PDF
conversion or global theme changes are needed.
The viewer also fixes an invalid click-counter timestamp that kept old clicks
from expiring and could turn a later double-click into line selection.

Preparing six pages ahead helps cover the original figure-decoding cost while
scrolling. This is an isolated reader setting, with the same 32-page cache; it
does not change global Sioyek preferences. The reader default remains three
pages ahead. In the short 21-PageDown test, the compact copy's p90 update time
was 125 ms at three pages ahead and 99 ms at six. The previous 398 MB fast copy
was 75 ms, while the original was 339 ms, both at three pages ahead. These are
one-run Xvfb software-OpenGL measurements with fixed IPC/capture overhead, not
desktop mouse-wheel FPS. Figure-heavy jumps can still incur the original
JPEG 2000 cost. Reports are `artifacts/reading-compact-scrolling-validation.json`
and `artifacts/compact-reading-mode-validation.json`.

A longer test with 80 PageDown actions exceeds the initial six-page buffer. With
the same 32-page cache and six pages ahead for both files, p90 update time drops
from **369 ms for the original to 81 ms for the compact copy**. Median updates
remain near 70–75 ms, dominated by already-cached pages and command/capture
overhead. This measures fewer delayed updates during sustained scrolling, not
faster decoding of the retained original figures. It is a single run per file
under isolated software OpenGL, so desktop results can differ.

### Optical Electronics: faster reading baseline

`artifacts/OPTICAL ELECTRONICS FOURTH EDITION-reading-fast.pdf` is the new full
744-page copy, rebuilt from the original with no manual page overrides:

```console
pdf-prep --clip-figures --black-text-fast \
  --protect-regions artifacts/contents-corrected-regions.json --no-migrate \
  "artifacts/OPTICAL ELECTRONICS FOURTH EDITION.pdf" \
  "artifacts/OPTICAL ELECTRONICS FOURTH EDITION-reading-fast.pdf"
```

The result is **397 MB (379 MiB)**. It has 387 constant-black foregrounds,
356 native-resolution figure foregrounds and one unchanged foreground. Paper is
white on 742 pages; the covers retain their backgrounds. All original masks and
background image streams, selectable text positions, page drawing instructions,
geometry and bookmarks are preserved. All **1.134 billion protected foreground
pixels** checked are exact, and sampled figures including 2-8 and 2-10 match the
original render at 300 dpi. Pages 6, 19 and 53 use black text, not false figure
regions. These checks validate the protected boxes, not perfect figure detection.

On 19 sampled pages, median cold MuPDF rendering at 150 dpi drops from **667 ms
to 98 ms**. In the **same installed Sioyek**, the 90th-percentile visible PageDown
update drops from **1,507 ms to 475 ms**; updates taking over 500 ms drop from
**16/42 to 2/42**. Median updates remain approximately 120 ms because many steps
only move within an already rendered page. This is reduced page-loading stutter,
not a claim that every scroll becomes three times faster.

The Sioyek test uses two fresh-viewer runs per PDF, identical zoom/window/cache
settings, an isolated Xvfb display and software OpenGL. Timing ends when displayed
pixels match the fully rendered reference, not when speculative background work
finishes. It includes IPC/capture overhead and does not measure physical-desktop
mouse-wheel frame rate. Reports are `artifacts/reading-fast-validation.json` and
`artifacts/reading-fast-scrolling-validation.json`. The source PDFs and the user's
running viewer, settings and annotation database are untouched.

### Local Sioyek pre-render fix

The sibling `sioyek` checkout also contains a renderer fix. Whole-page prefetch
used a different cache key from visible rendering, so a prepared page was decoded
again when shown. Prefetch also ran ahead of visible-page requests in the worker
queue. The fix matches whole-page/sliced keys and same-width zoom, queues distant
prefetch first, and promotes visible requests over queued duplicates.

With the same reading PDF and the same source-build version, the visible-update
90th percentile falls from **435 ms to 118 ms**, with **0/42 updates over 500 ms**.
The build and focused checks for unsliced/sliced, tall-page and same-width cases
pass. The installed system binary has **not** been replaced.

To try the locally built viewer on this Hyprland/Wayland desktop, from the
`pdf-prep` directory:

```console
PATH="$(pwd)/../sioyek/build:$PATH" QT_QPA_PLATFORM=wayland \
  pdf-prep --read "artifacts/OPTICAL ELECTRONICS FOURTH EDITION-reading-fast.pdf"
```

This opts into the local viewer only for that command and the temporary reader
cache profile; normal annotations still use your usual database. Opening the new
PDF in the installed viewer already provides the PDF-side speed improvement.

On an X11 desktop, use `QT_QPA_PLATFORM=xcb` only with a working `DISPLAY`.
The headless benchmarks used Xvfb, which does not establish that the desktop's
Xwayland connection works. A `could not connect to display` error is not evidence
that `libxcb-cursor` is missing; check the connection before installing libraries.
The local binary is required here because this machine's installed Sioyek wrapper
forces `xcb` even when `QT_QPA_PLATFORM=wayland` is supplied.

The local build now explicitly requests desktop OpenGL 3.3 for its shared and
page-rendering contexts. Previously, native Wayland selected OpenGL ES but received
a 3.3 request, causing context creation to fail. Pages 76 and 83 now render in a
mapped native Wayland window, with the 32-page cache/three-page pre-render profile
verified. See `artifacts/reader-wayland-validation.json`. X11 rendering also passes
a separate isolated smoke test. No display sockets or global settings were changed.

## Conservative edge cleanup

`--clean-edges` hides confidently detected scan-border lines and tiny specks with
small white PDF vector overlays. It does not crop, resize, rasterize, re-encode
images or remove selectable text. Only the outer 3% of the page, capped at 12 PDF
points, is considered. Padded text, protected figures and annotations are excluded;
marks connected to interior content and uncertain nonwhite areas are left alone.
This is deliberately not a general-purpose eraser for interior marks.

Combine it with whitening and faster black text using the original figure map:

```console
pdf-prep --clip-figures --black-text-fast --clean-edges \
  --protect-regions figures.json --no-migrate original.pdf reading-clean.pdf
```

On an already prepared PDF it runs alone, without repeating the other stages:

```console
pdf-prep --clean-edges --protect-regions prepared-figures.json --no-migrate \
  reading-fast.pdf reading-clean.pdf
```

The map must match the input PDF's checksum. A separate output is required;
in-place cleanup, OCR, legacy downsampling, native raster cropping and compression-only
cannot be combined with this mode. Original image streams and page geometry remain
intact; previous drawing streams are retained under the overlays. Running edge
cleanup again does not add further overlays to already cleaned pages. Detection
is conservative, not guaranteed perfect: inspect the result and keep the original.

### Optical Electronics: cleaned reading copy

The latest copy is `artifacts/OPTICAL ELECTRONICS FOURTH EDITION-reading-clean.pdf`.
It cleans borders on **734 of 744 pages** using 18,240 small vector overlays;
10 already-clean or uncertain pages are left alone. A few ambiguous short ticks
remain intentionally, rather than risking meaningful content. The file grows by
only **0.81 MiB (0.21%)**, from 378.64 to 379.45 MiB, with no image re-encoding.

To repeat only the cleanup on the verified fast copy:

```console
pdf-prep --clean-edges --protect-regions artifacts/reading-fast-regions.json \
  --no-migrate "artifacts/OPTICAL ELECTRONICS FOURTH EDITION-reading-fast.pdf" \
  "artifacts/OPTICAL ELECTRONICS FOURTH EDITION-reading-clean.pdf"
```

All pages render. Validation compares original image/mask bytes, text positions,
geometry and existing drawing streams, then checks rendered changes against the
overlay rectangles and padded text/figure/annotation guards. The report is
`artifacts/reading-clean-validation.json`. The combined three-stage command is also
checked on an 11-page original-scan preview, and synthetic checks cover rotations,
CropBox offsets, corner-connected borders, near-edge text and failure cleanup.

To read the latest copy with the local Wayland-compatible pre-render fix:

```console
PATH="$(pwd)/../sioyek/build:$PATH" QT_QPA_PLATFORM=wayland \
  pdf-prep --read "artifacts/OPTICAL ELECTRONICS FOURTH EDITION-reading-clean.pdf"
```

## Compressed-image clipping

**The old stage 1 speed pass can damage pictures.** The high-resolution foreground
is not always just ink colour: Figures 2-8 and 2-10 in *Optical Electronics* contain
photographic detail there too. Keeping the original stencil and low-resolution
background did not preserve that detail. Restart from the **original PDF**, not
a stage 1/2/3 output; lost detail cannot be recovered from those copies.

Use **PDF clipping**, not raster extraction/re-encoding, to retain original picture
detail without a huge file. Original JPEG 2000 images, stencils, masks and JBIG2
globals retain their encoded bytes. A small Form XObject draws the original background
and clips a white overlay to areas outside protected regions. Foregrounds,
selectable text and page drawing instructions remain unchanged.

For whole figures with their original paper tint, generate a protection map and use:

```console
pdf-prep-regions original.pdf figures.json \
  --model-name PP-DocLayout-S --model-dir /path/to/PP-DocLayout-S_infer --padding .012
pdf-prep --clip-figures --protect-regions figures.json --no-migrate \
  original.pdf original-clipped-white.pdf
```

This retains the original paper tint within protected figure rectangles. Unprotected
paper becomes white without changing image pixels. Even backgrounds on paper-only
pages keep their original compressed image as an unused resource, so clipping does
not discard the original data. Review detection boxes: a missed box can hide a faint
picture behind white paper, but its compressed data is still retained. Unlisted
pages stay unchanged; unsafe shared images, complex colour/transparency contexts
and uncertain covers are skipped. Repeating whitening does not rasterize a clipped
background. Overlapping boxes stay protected. Clipping only the white overlay avoids
changing an image renderer's sampling at newly cropped image boundaries.

`--clip-figures` alone disables raster speed passes, OCR and the separate compression pass.
It requires a checksum-matched `--protect-regions` map and cannot be combined with
`--crop-figures`, `--lossy-fast`, `--compress-only`, `--force-ocr` or `--no-whiten`.
It can be combined with `--black-text-fast` for the reading copy described above.
It uses only local figure detection; there is no AI image regeneration.

**Clipping is not a scrolling-speed fix.** The original high-resolution foreground
still uses JPEG 2000 and must be decoded. This mode establishes a small, faithful
white-paper baseline; rendering speed and viewer caching need separate work.

Raster speed passes are no longer automatic. `--crop-figures` explicitly opts into
the earlier native RGB/Flate raster-transcoding experiment, which can enlarge a
book by orders of magnitude; `--lossy-fast` opts into global downsampling, which can
damage figures. Neither is needed for clipping. `--jobs 1` limits verification CPU
use; clipping verification otherwise uses up to four processes. Every output page
is rendered, and the command checks the original checksum before publishing.

### Compressed-clipping trial

The full 744-page original becomes **47.09 MB**, versus **46.87 MB** before
(only **0.47% larger**), not gigabytes. All 1,488 original colour image streams and
744 foreground/mask groups retain their encoded bytes. All pages render; selectable
text, word positions, geometry, page drawing instructions and the table of contents
match the original. Paper is whitened on 742 pages; the two covers remain untouched.

Protected regions on 14 sampled pages, including **page 76 / Figure 2-8** and
**page 83 / Figure 2-10**, render pixel-identically at 150 dpi. This does **not**
prove the layout detector found every picture; inspect its boxes and the output.
The preview benchmark shows no meaningful scrolling-speed improvement.

Use `artifacts/OPTICAL ELECTRONICS FOURTH EDITION-compressed-clips-white.pdf`;
`artifacts/figures-2-8-2-10-compressed-clips.pdf` is the approximately 126 KiB preview.
`artifacts/compressed-clips-book-validation.json` records the checks. The original
and earlier trials are not replaced.

The latest review copy is
`artifacts/OPTICAL ELECTRONICS FOURTH EDITION-compressed-clips-contents-fixed.pdf`.
It removes four oversized contents-text detections on printed pages VIII, XII and
XIII (PDF pages 12, 16 and 17); other page image/Form streams stay unchanged.
`artifacts/contents-viii-xii-xiii-white.pdf` previews those three pages, and
`artifacts/contents-fix-validation.json` records preserved image/mask bytes, text,
figure rendering and corrected-page checks.

## Speed stage: black text, native figure pixels

Caching alone did not noticeably improve this book in the user's Sioyek session.
The main cost is decoding the full-page JPEG 2000 foreground colour field, around
half a second per sampled page, not the much faster JBIG2 text stencil.

`--black-text-fast` explicitly accepts **black scanned text** outside the protected
figure regions. Text shapes still come from the **original compressed bitonal mask**.
On figure pages, native RGB pixels inside protected rectangles, plus a 32-pixel
interpolation margin, stay unchanged; the rest of the colour field becomes black.
That sparse field uses lossless PNG-Flate compression at the original dimensions.
Pages without figures need only a constant black colour field. Background image
streams, existing white-paper clipping, OCR, geometry and page drawing instructions
are not rewritten. This is not a lossless change to text colour, and can enlarge
the PDF, but does not downsample or quantize protected figures.

For an already-whitened PDF, use a region map bound to its checksum:

```console
pdf-prep --black-text-fast --protect-regions artifacts/black-text-fast-regions.json \
  --no-migrate "artifacts/OPTICAL ELECTRONICS FOURTH EDITION-compressed-clips-contents-fixed.pdf" \
  "artifacts/OPTICAL ELECTRONICS FOURTH EDITION-black-text-fast.pdf"
```

Open the resulting PDF normally in Sioyek; a special cache profile is not required.
The mode requires a separate output and permits only `--clip-figures` as an
additional processing mode. Missing
page maps, near-full-page artwork, unsupported masks/colour contexts and already
converted fields stay unchanged. Protection is only as complete as the supplied
map: review it before applying this option to another book. `--no-migrate` keeps
the original book's Sioyek annotations attached to the original checksum.

For a confirmed false picture detection, `--text-only-pages 53` overrides **both**
the layout boxes and the paper-picture heuristic on that 1-based PDF page. The same
override can be stored as `"text_only_pages": [53]` in a checksum-matched region map,
or supplied to `pdf-prep-regions` during detection. Use it only for pages known to
contain no figures: it deliberately suppresses picture protection. Redo whitening
from the original scan when correcting an existing grey protected patch; changing
the black-text colour field alone does not remove a previous background clip.

The chapter-opening page printed **35** is PDF page **53** in this book; its shaded
chapter-number badge was misclassified as a picture. PDF page 54 (printed 36) contains
the real Figure 2-1 and must stay protected. The chapter-corrected full-book output
is `artifacts/OPTICAL ELECTRONICS FOURTH EDITION-black-text-fast-chapter-fixed.pdf`;
only PDF page 53 changes. That was a manual correction, not a general detector fix.
The earlier output and its benchmark reports remain intact.

### Automatic text-region validation

The command now checks layout boxes against the existing OCR and rendered page,
including boxes from older maps. It rejects a dense text block only when at least
40 alphabetic words on 10 rows account for at least 99% of its visible ink. An
unexplained connected stroke, nearby figure/table caption or missing OCR keeps the
region protected. Both columns of the publisher's book list on PDF page 6 pass
this check without any page-number override.

Chapter badges receive a separate conservative check: a small square beside a
large heading on a recognized chapter introduction must contain a locally
recognized number matching that chapter. Tesseract must report confidence of at
least 85; missing Tesseract, failed recognition and uncertain artwork stay protected.
PDF pages 19 and 53 now pass automatically. This OCR checks the badge only; it
does not replace the document's text layer or redraw figures.

Confirmed text/badge boxes also exclude those areas from the paper-picture
heuristic, so it cannot immediately reintroduce the same grey patch. Shared
backgrounds remain conservative: an uncertain use keeps picture protection.
The checks apply to clipping, raster whitening/native crops and black-text-fast.
There are no book-specific page numbers in the detector.

Restart whitening from the original scan, using the existing map:

```console
pdf-prep --clip-figures --protect-regions artifacts/contents-corrected-regions.json \
  --no-migrate "artifacts/OPTICAL ELECTRONICS FOURTH EDITION.pdf" \
  "artifacts/OPTICAL ELECTRONICS FOURTH EDITION-auto-white.pdf"
```

Clipping alone performs the white-paper stage. Add `--black-text-fast` to the same
command to also accelerate rendering using the original map. If running the speed
stage separately on a new white PDF, its map must match that file's checksum.
Neither mode retroactively repairs an older clipped background. Manual
`--text-only-pages` remains available for confirmed misses.

`artifacts/automatic-text-regions-preview.pdf` contains original PDF pages
6, 19, 53, 54, 76, 83, 200, 418, 456, 600 and 700, processed without manual overrides.
`artifacts/automatic-text-regions-validation.json` records the whole-book layout
review and checks of protected native figure pixels, original masks and OCR.

The full 744-page output is **403 MB (384 MiB)**, versus 47 MB for the clipped source:
374 pages use black text fields, 369 preserve native figure fields, and the front
cover stays unchanged. All original masks and background streams are byte-identical,
all page text positions/drawing instructions/geometry/TOC are unchanged, and all
1.147 billion protected foreground RGB pixels checked are exact. Figures 2-8 and
2-10 render pixel-identically at 300 dpi. Different image decoder subsampling can
cause small colour-rounding differences at lower display resolutions.

On 15 sampled pages of the full book, MuPDF cold-render median drops from **690 ms
to 111 ms**. An isolated Sioyek test of the full book, at the same zoom with five
cached pages and no pre-rendering for both variants, drops median page readiness
from **3,381 ms to 840 ms**. That test includes IPC overhead and uses software OpenGL
in Xvfb; it is **not** a measurement of physical-desktop scrolling. Reports are in
`artifacts/black-text-fast-book-validation.json` and
`artifacts/black-text-fast-sioyek-benchmark.json`. Both input PDFs remain unchanged.

## Viewer-only experiment: Sioyek caching

Start with a viewer-only optimization, not another raster conversion:

```console
pdf-prep --read "artifacts/OPTICAL ELECTRONICS FOURTH EDITION-compressed-clips-contents-fixed.pdf"
```

On Linux this opens a separate Sioyek instance with **32 cached rendered pages**
and **three pages pre-rendered ahead**, compared with this machine's untuned
settings of five cached pages and no pre-rendering. The
[Sioyek configuration documentation](https://sioyek-documentation.readthedocs.io/en/latest/configuration.html#prerendered-page-count)
describes how pre-rendering reduces flicker at the cost of memory.

`--read` does not rewrite the PDF: no downsampling, recompression, OCR or larger output file.
A temporary `XDG_CONFIG_HOME` sources your usual Sioyek preferences and copies your
keybindings, then applies only the two cache settings before the renderer starts.
Global preferences stay untouched; the normal data directory and annotation
database stay in use. The terminal command remains running until the reader closes,
then removes its temporary profile. Sioyek must already be on `PATH`.

This is a named instance, printed when it starts. External Python extensions that
send commands to Sioyek's default instance need to target that name instead; native
viewer controls and annotations continue to work normally.

Use `--reader-page 76` to start at Figure 2-8, `--reader-cache-pages 16` for a smaller
texture cache, or `--reader-prerender-pages 1` to reduce background CPU work.
`--dry-run --read` writes nothing and launches nothing. Reader options cannot be
combined with PDF-processing options or an output path.

Rendering the corrected book at 150 dpi on 15 sampled pages measures **763 ms**
median on the first render versus **62 ms** on an immediate repeat with decoded
images cached. This is a MuPDF benchmark, **not** a measured 12× Sioyek scrolling
speedup. Pre-rendering hides work while you read; retained page textures avoid
repeated decoding when revisiting pages. A distant jump or new zoom can still
need an initial decode, and memory use grows with cache count and zoom. Inspect
actual scrolling before changing your global configuration.

The installed Sioyek was checked in an isolated virtual display: both cache settings
and the usual annotation-rendering preference are confirmed, pages 76 and 83 render,
and closing it removes the private profile. PDF bytes and the user's preferences and
keybindings stay identical. See `artifacts/reader-speed-validation.json`.

### Earlier native-raster experiment (not recommended)

The full original 744-page book has been rebuilt: all 744 foregrounds use native
crops, 301 backgrounds become white, 441 retain original picture/background crops,
and the two covers keep their original backgrounds. All pages render; all
1,156,051 selectable characters, word positions, page geometry, drawing instructions
and the table of contents match the original.

Native-sample checks cover 14 PDF pages, including **page 76 / Figure 2-8** and
**page 83 / Figure 2-10**. Across 277 crops, 425,257,452 decoded RGB channel values
match their original source pixels; all 8,806,702 visible stencil pixels in those
pages are retained. Synthetic checks also cover inverted and bitonal soft masks,
shared/nested forms, rotation, interpolation and unsupported transparency.

At 150 dpi, the 14-page median drops from **696 ms to 154 ms** (about **4.5× faster**).
This is a rendering benchmark, not a guarantee about viewer cache, initial loading
or scroll animation. Plain Flate was kept for decoding speed: an experimental TIFF
predictor saved storage but rendered the same figures more slowly.

The RGB/Flate transcoding choice produced **3.86 GiB**, versus the original 44.7 MiB.
This is not an inherent cost of native image quality: compressed-image clipping
preserves the original encodings instead. The large trial is not a practical reader.
For inspection, `artifacts/figures-2-8-2-10-native-white.pdf` is a two-page, 11.1 MiB
preview. The full book is
`artifacts/OPTICAL ELECTRONICS FOURTH EDITION-native-white.pdf`;
`artifacts/native-book-validation.json` records the checks. No original or earlier
stage output was replaced. Review the figures and actual viewer scrolling before
using the large copy as your everyday reader.

```console
$ pdf-prep --dry-run "Optical Alignment I.pdf"
260 pages, Internet Archive (MRC layered), about 359 dpi
  text layer : present (256 of 260 pages)
  paper      : tinted
  plan       : whiten, compress
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
JPEG 2000 that supplies ink colour **and sometimes picture detail**. PDF
allows explicit image masks (`/Mask`) and soft masks (`/SMask`) to differ in pixel
size from the image they mask, so that colour field can shrink to a thumbnail
while text stencil shapes remain sharp, but photographs can lose detail. This is
why that earlier optimization is no longer the default. Images inside nested Form XObjects are
processed too, and shared images are rewritten only once.

The historical thumbnail pass measured **596 ms/page → 120 ms/page** on one
328-page book. That result must not be generalized to picture fidelity.

### Historical stage 1: mask handling (lossy)

Older LuraDocument scans use explicit `/Mask` streams. The original speed pass
handled only `/SMask`, so it skipped their foregrounds. Both representations now
keep their original mask streams, image placement and selectable text while the
ink-colour image shrinks. MuPDF decodes the colour image, including its PDF colour
space and decode settings; this also supports encodings other than JPEG 2000.
Images that cannot be decoded cleanly are left unchanged and reported, including
truncated images that MuPDF would otherwise pad with invented pixels.

Validated on the 744-page *Optical Electronics, Fourth Edition* scan: all 744
foregrounds shrink, and the file goes from **44.7 MiB to 25.5 MiB (43% smaller)**.
All pages render; the original masks, photo/paper background streams, selectable
text and word positions, page geometry and table of contents remain unchanged.
On 12 sampled pages rendered at 150 dpi, median rendering time drops from
**1,024 ms to about 110 ms per page**. Viewer performance depends on its cache and
rendering settings.

To use the speed improvement independently of whitening:

```console
pdf-prep --lossy-fast --no-whiten --no-ocr --no-migrate in.pdf in-fast.pdf
```

Shrinking the ink-colour field is approximate: the original letter shapes remain
intact, but colour and actual photographic detail can change substantially.
The historical timings and mask/background checks did **not** establish original
figure fidelity. Use the quality-first mode above instead.

### Stage 2: picture-aware white paper

The stage 2/3 measurements below used the already-lossy stage 1 input. Neither
stage restores detail lost during downsampling; they are historical comparisons,
not quality guarantees for the original figures.

Whitening now edits the original MRC background object, not the rendered page.
It detects local dark or coloured picture regions and turns the surrounding
paper into exact RGB `(255, 255, 255)`. Local contrast avoids treating gradual
scanner illumination changes as photographs. Long, thin scanner-edge shadows
are excluded. The foreground, its original stencil and the existing selectable
text are untouched.

Protected picture pixels keep their exact decoded RGB values: there is no
white-balance, gamma adjustment or JPEG recompression of photos. A paper-only
background becomes a single white pixel, retaining its original page placement.
Backgrounds containing protected pictures use lossless PNG-predictor/Flate
encoding. Dark/saturated covers, non-MRC pages, non-bitonal transparency and
uncertain large artwork are left unchanged. Shared background objects union
the protected regions from all their pages; incompatible uses remain unchanged.

Lossless photo encoding can still increase the size compared with the speed-only
output. The separate stage 3 compression pass reduces this overhead without
sacrificing image detail during whitening.

Validated on all 744 pages of *Optical Electronics, Fourth Edition*: **454**
backgrounds become flat white, **288** are whitened around protected regions,
and only the front/back covers remain unchanged. All pages render without
warnings. All 744 foreground and stencil streams, 1,156,051 selectable text
characters, word positions, page geometry and table of contents match stage 1.
Over 10 million protected pixels match exactly; all unprotected paper pixels
are pure white. Synthetic checks cover explicit and bitonal soft masks,
shared/rotated placements, colour-key transparency, invalid maps and repeat runs.

The resulting book is **29.6 MiB**, versus **44.7 MiB original** and **25.5 MiB
speed-only**: 34% below the original, but 16% above stage 1 because of lossless
picture encoding. On the same 12 pages at 150 dpi, median rendering drops from
**109 ms (stage 1) to 66 ms (stage 2)**. The validation report and a six-page
preview are in the local ignored `artifacts/` directory. The full-book result
uses the normal tonal detector; optional AI protection was tested separately
on selected pages and the sample, not inferred over the entire book.

To whiten a speed-optimized copy without regenerating its text layer or moving
viewer annotations:

```console
pdf-prep --no-ocr --no-migrate in-fast.pdf in-white.pdf
```

### Stage 3: lossless compact encoding

The compression pass compares maximum-level plain Flate against PNG-predictor
Flate for 8-bit DeviceRGB/DeviceGray images. Smooth ink-colour thumbnails often
benefit from prediction, while sparse white backgrounds can be smaller without
it. Each stream keeps its dimensions, colour space, decode settings, masks and
placement. Existing JPEG, JPEG 2000 and JBIG2 streams are preserved, not decoded
and recompressed into a larger format. There is no additional downsampling,
lossy quantization, cropping or OCR.

Content and font streams can be recompressed without changing their decoded
bytes. Stencils, soft/explicit masks and JBIG2 global streams keep their original
encoded bytes; uncertain or unsupported streams are skipped and reported.
Small streams are not rewritten for negligible savings. The candidate file is
only accepted if the **whole PDF is smaller**; otherwise the input bytes are
kept. Repeating compression on the result does not re-encode unchanged streams.

Compression runs automatically after planned whitening, speed or OCR work,
and when the survey finds potentially improvable Flate images. `--no-compress`
skips it. To compact an existing white/fast PDF independently:

```console
pdf-prep --compress-only --no-migrate in-white.pdf in-compact.pdf
```

`--compress-only` disables whitening, ink-colour resizing and OCR, even if the
survey would otherwise plan them. `--dry-run` remains non-writing. This pass
does not need any additional dependencies or neural-network models.

On the full 744-page *Optical Electronics* book, stage 3 reduces the white copy
from **29.6 MiB to 26.3 MiB (11% smaller)**, or **41% smaller than the original
44.7 MiB scan**. It losslessly re-encodes 1,028 images and recompresses 277 other
streams. All 1,488 image sample arrays, all 744 encoded stencil streams and the
decoded page drawing instructions remain identical. All 744 pages render
pixel-for-pixel identically at 72 dpi; the same 12 sampled pages are also
pixel-identical at 150 dpi. Selectable text, word positions, geometry and the
table of contents remain unchanged. Median sample rendering is **59 ms/page
before and after**: this stage saves storage without trading away scroll speed.

Additional checks cover explicit/soft masks, nested/shared resources, rotated
placements, inverted decode arrays, colour-key transparency, uncompressed
images, damaged streams, stencil globals, incompressible inputs and repeated
runs. `artifacts/stage3-validation.json` records the local validation results.

Stage 4 remains: integrated before/after rendering measurements and stronger
outcome checks in the command itself. Stage 3 validation currently runs
separately, with reports saved under the local ignored `artifacts/` directory.

## What it does

| pass | when | what |
|---|---|---|
| **whiten** | paper is tinted | rewrites the background layer only; text mask untouched |
| **clip-figures** | explicitly requested with a protection map | whitens outside original compressed images; no raster conversion, OCR or compact pass |
| **crop-figures** | explicitly requested with a protection map | experimental native RGB/Flate raster crops; potentially huge files |
| **lossy-fast** | explicitly requested | downsampling that can damage picture detail |
| **ocr** | no real text layer | `ocrmypdf`, with `--oversample` chosen from the scan's dpi; a sideways page (lettering running up or down the sheet in more than one column) is read a second time turned level and left standing level, because Sioyek cannot select lettering that runs up or down. `--no-turn`, or Sioyek notes on the file, keep the page sideways with its text placed under the lettering |
| **compress** | previous work runs or potentially improvable Flate images are sampled | selects smaller lossless encodings; never increases the PDF size |

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

`--no-ocr`, `--no-whiten`, `--no-fast`, `--no-compress` skip a pass; `--no-turn` leaves sideways pages sideways; `--force-ocr` and `--force-whiten`
run one the survey did not plan. Verification renders every output page.

### Optional offline layout detector

For very pale figures that tonal detection might miss, `pdf-prep-regions` uses
[PaddleOCR layout detection](https://www.paddleocr.ai/main/en/version3.x/module_usage/layout_detection.html)
to produce additional protected image/chart/table rectangles. This is optional:
the normal command does not import Paddle, need a GPU or download model weights.
Install the optional dependencies in a separate environment (Python 3.12 was
tested locally) and supply an
already-downloaded `PP-DocLayout_plus-L` model directory:

```console
pip install '.[layout]'
pdf-prep-regions in-fast.pdf regions.json \
  --model-dir /path/to/PP-DocLayout_plus-L --pages 200,600,700
pdf-prep --no-ocr --no-migrate --protect-regions regions.json in-fast.pdf in-white.pdf
```

Omit `--pages` to detect every page; CPU inference over a long book can take many
minutes. On this machine the existing offline Paddle environment and weights
were exercised on pages 6, 200, 600 and 700 of *Optical Electronics*. The helper
uses only local model weights, with model-source connectivity checks disabled.
The core Nix package deliberately does not include this heavy optional stack;
run `pdf_prep_regions.py` using a separately provisioned Paddle environment.

`PP-DocLayout-S` is a smaller, faster alternative; pass its model name and local
directory explicitly. An optional `--render-proxy in-fast.pdf` accelerates detection
using a geometrically matching copy. Only detection reads the proxy; the map's
checksum belongs to the original input and processing still reads original pixels.
The helper checks page counts, sizes, boxes and rotations, and detects input changes.
A proxy cannot supply missing original image detail, and can affect detection quality.

Maps are tied to the **exact input file** by its checksum and page count. Make a
map after the speed pass if whitening that output. Coordinates are normalized
to the rendered page, including rotation; the tool maps them back through the
background image's placement. Selected pages use both the added rectangles and
the normal detector; other pages still use the normal detector. You can also
provide a hand-reviewed map with this format:

```json
{
  "version": 1,
  "source_md5": "<MD5 of the exact input PDF>",
  "page_count": 744,
  "pages": {"200": [[0.15, 0.07, 0.83, 0.28]]}
}
```

Rectangles are `[left, top, right, bottom]` within `[0, 1]`, with 1-based page
keys. Pixels inside protected boxes are retained, including original paper tint;
boxes confidently verified as text or chapter decoration are rejected as described
under automatic text-region validation.
Review model boxes: a missed figure cannot be protected, and an oversized box
leaves more tinted paper. This is detection, not generative reconstruction of
scientific diagrams or formulae.

Contents pages can be mislabelled as page-sized images or tables. Both the layout
helper and whitening reject oversized regions when the existing OCR confidently
identifies a contents heading and many numbered entries/page references. Small
figure boxes, real tables on other pages and uncertain pages without sufficient
OCR stay protected. The helper and processing passes also perform the ink/OCR and
chapter-badge checks described above. Newly generated maps retain rejected boxes
in `rejected_text_regions` so the command can revalidate them and suppress the
paper heuristic at the same location. This also works with older maps; no original
compressed image or mask data is discarded by clipping.

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

The background is median-filtered at thumbnail scale to suppress small scan
noise. Connected regions darker than their nearby bright envelope, or more
saturated than the dominant paper colour, get padded protection rectangles.
Thin edge bands are treated as scanner shadows, not figures. This is a tonal
heuristic, not a semantic guarantee; optional layout maps can extend protection.

**Where is the paper, and what colour is it?** A sufficiently dominant, bright,
not-too-saturated luminance mode supplies one shared set of paper pixels. Their
median RGB colour is used only for detection. Instead of globally stretching
tones, whitening replaces unprotected background pixels with pure white and
leaves protected pixels alone. An uncertain paper estimate means no rewrite.

## Limitations

- **Picture detection is not a guarantee.** Very faint, small or edge-touching
  features can be missed. Review a sample and add protected rectangles for
  important pale illustrations before processing irreplaceable files.
- Only Internet Archive-style MRC scans benefit from `whiten` and `fast`. Everything
  else is surveyed and left alone.
- Lossless encoding of photographic regions may outweigh the space saved by
  flattening paper. Compression never enlarges its input, but it cannot guarantee
  that whitening plus compression will be smaller than the original encoding.
- Covers and uncertain artwork can remain tinted. Scanner marks already baked
  into the ink stencil remain too: whitening does not erase or crop ink.

## Verifying its work

After writing, it re-opens the result, renders every page and reports how many still
have tinted paper. Measure per channel if you check this yourself: a greyscale test
passes a page whose paper has gone *yellow*, because red is already at 255.

Decode warnings during whitening leave the uncertain image unchanged and are
reported. Running the whitening pass again on unchanged white/protected pixels
does not re-encode them.

## Licence

MIT.
