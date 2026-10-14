"""Conservative scan-edge cleanup without changing page images or text."""

import math

import fitz
import numpy as np


def protected_pixels(page, regions, width, height):
    protected = np.zeros((height, width), dtype=bool)
    scale_x = width / page.rect.width
    scale_y = height / page.rect.height
    rectangles = []
    for word in page.get_text("words"):
        rectangle = fitz.Rect(word[:4]) * page.rotation_matrix
        padding = max(2.5, min(4, rectangle.height * 0.2))
        rectangles.append(rectangle + (-padding, -padding, padding, padding))
    for left, top, right, bottom in regions:
        rectangle = fitz.Rect(left * page.rect.width, top * page.rect.height,
                              right * page.rect.width, bottom * page.rect.height)
        rectangles.append(rectangle + (-6, -6, 6, 6))
    for annotation in page.annots() or ():
        rectangle = annotation.rect * page.rotation_matrix
        rectangles.append(rectangle + (-2, -2, 2, 2))
    for rectangle in rectangles:
        left = max(0, math.floor(rectangle.x0 * scale_x))
        top = max(0, math.floor(rectangle.y0 * scale_y))
        right = min(width, math.ceil(rectangle.x1 * scale_x))
        bottom = min(height, math.ceil(rectangle.y1 * scale_y))
        if right > left and bottom > top:
            protected[top:bottom, left:right] = True
    return protected


def components(active):
    remaining = active.copy()
    height, width = remaining.shape
    for seed_y, seed_x in np.argwhere(remaining):
        if not remaining[seed_y, seed_x]:
            continue
        pending = [(int(seed_y), int(seed_x))]
        remaining[seed_y, seed_x] = False
        points = []
        while pending:
            current_y, current_x = pending.pop()
            points.append((current_y, current_x))
            for next_y in range(max(0, current_y - 1), min(height, current_y + 2)):
                for next_x in range(max(0, current_x - 1), min(width, current_x + 2)):
                    if remaining[next_y, next_x]:
                        remaining[next_y, next_x] = False
                        pending.append((next_y, next_x))
        yield np.asarray(points, dtype=np.int32)


def edge_rectangles(page, regions):
    """Return small white-overlay boxes around confidently isolated edge debris."""
    if page.rect.is_empty or page.rect.is_infinite:
        return []
    if math.ceil(page.rect.width * 2) * math.ceil(page.rect.height * 2) > 16_000_000:
        return []
    pixmap = page.get_pixmap(dpi=144, colorspace=fitz.csRGB, alpha=False, annots=False)
    rgb = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width, 3)
    height, width = rgb.shape[:2]
    scale_x = width / page.rect.width
    scale_y = height / page.rect.height
    band_points = min(12.0, min(page.rect.width, page.rect.height) * 0.03)
    band_x = max(1, math.floor(band_points * scale_x))
    band_y = max(1, math.floor(band_points * scale_y))
    ink = rgb.min(axis=2) < 245
    white = rgb.min(axis=2) >= 250
    coloured = rgb.max(axis=2).astype(np.int16) - rgb.min(axis=2) > 24
    protected = protected_pixels(page, regions, width, height)
    if not page.get_text("words") and not regions:
        interior = ink[band_y:height - band_y, band_x:width - band_x]
        if not interior.size or interior.mean() > 0.005:
            return []
    frame = np.ones_like(ink)
    frame[band_y:height - band_y, band_x:width - band_x] = False
    interior = ink & ~frame
    touches_interior = np.zeros_like(ink)
    touches_interior[1:] |= interior[:-1]
    touches_interior[:-1] |= interior[1:]
    touches_interior[:, 1:] |= interior[:, :-1]
    touches_interior[:, :-1] |= interior[:, 1:]
    touches_interior[1:, 1:] |= interior[:-1, :-1]
    touches_interior[1:, :-1] |= interior[:-1, 1:]
    touches_interior[:-1, 1:] |= interior[1:, :-1]
    touches_interior[:-1, :-1] |= interior[1:, 1:]
    eligible = ink & frame
    for points in components(eligible):
        rows, columns = points[:, 0], points[:, 1]
        if (touches_interior[rows, columns].any()
                or protected[rows, columns].any() or coloured[rows, columns].any()):
            eligible[rows, columns] = False
    proposed = []
    edges = (
        (ink, eligible, white, coloured, protected, band_x, band_y, scale_x, scale_y,
         lambda left, top, right, bottom: (left, top, right, bottom)),
        (ink[:, ::-1], eligible[:, ::-1], white[:, ::-1], coloured[:, ::-1], protected[:, ::-1], band_x, band_y, scale_x, scale_y,
         lambda left, top, right, bottom: (width - right, top, width - left, bottom)),
        (ink.T, eligible.T, white.T, coloured.T, protected.T, band_y, band_x, scale_y, scale_x,
         lambda left, top, right, bottom: (top, left, bottom, right)),
        (ink[::-1].T, eligible[::-1].T, white[::-1].T, coloured[::-1].T, protected[::-1].T, band_y, band_x, scale_y, scale_x,
         lambda left, top, right, bottom: (top, height - right, bottom, height - left)),
    )
    for edge_ink, edge_eligible, edge_white, edge_coloured, edge_guard, band, corner, depth_scale, along_scale, transform in edges:
        along_length = edge_ink.shape[0]
        strip = edge_eligible[:, :band]
        halo = min(edge_ink.shape[1], band + math.ceil(4 * depth_scale))
        if edge_white[:, :halo].mean() < 0.7:
            continue
        candidates = []
        selected = np.zeros_like(strip)
        for points in components(strip):
            along, depth = points[:, 0], points[:, 1]
            if edge_guard[along, depth].any():
                continue
            if edge_coloured[along, depth].any():
                continue
            inner = (depth == band - 1) & (along >= corner) & (along < along_length - corner)
            if inner.any():
                continue
            minimum_depth = int(depth.min())
            depth_extent = (int(depth.max()) - minimum_depth + 1) / depth_scale
            along_extent = (int(along.max()) - int(along.min()) + 1) / along_scale
            tiny = max(depth_extent, along_extent) <= 2
            central = (along >= corner) & (along < along_length - corner)
            central_depth = depth[central] if central.any() else depth
            line_depth = (int(central_depth.max()) - int(central_depth.min()) + 1) / depth_scale
            thin = line_depth <= 6 and along_extent >= max(12, line_depth * 4)
            if not (tiny or thin):
                continue
            selected[along, depth] = True
            candidates.append(points)
        for points in candidates:
            along, depth = points[:, 0], points[:, 1]
            chunk = max(1, math.floor(18 * along_scale))
            start = int(along.min())
            stop = int(along.max()) + 1
            pending = [(first, min(stop, first + chunk)) for first in range(start, stop, chunk)]
            while pending:
                first, last = pending.pop()
                members = (along >= first) & (along < last)
                if not members.any():
                    continue
                left = max(0, int(depth[members].min()) - 1)
                right = min(band, int(depth[members].max()) + 2)
                top = max(0, int(along[members].min()) - 1)
                bottom = min(along_length, int(along[members].max()) + 2)
                if (edge_guard[top:bottom, left:right].any()
                        or (edge_ink[top:bottom, left:right] & ~selected[top:bottom, left:right]).any()):
                    if last - first > 1:
                        midpoint = (first + last) // 2
                        pending.extend(((first, midpoint), (midpoint, last)))
                    continue
                paper_left = max(0, left - math.ceil(3 * depth_scale))
                paper_right = min(edge_ink.shape[1], right + math.ceil(3 * depth_scale))
                paper_top = max(0, top - math.ceil(3 * along_scale))
                paper_bottom = min(along_length, bottom + math.ceil(3 * along_scale))
                if edge_white[paper_top:paper_bottom, paper_left:paper_right].mean() < 0.55:
                    selected_depths = np.flatnonzero(selected[top:bottom].any(axis=0))
                    gutter_left = max(right, int(selected_depths.max()) + 2) if selected_depths.size else right
                    gutter_right = min(edge_ink.shape[1], gutter_left + math.ceil(3 * depth_scale))
                    gutter = edge_white[top:bottom, gutter_left:gutter_right]
                    if gutter.shape[1] < math.ceil(2 * depth_scale) or gutter.mean() < 0.95:
                        continue
                proposed.append(transform(left, top, right, bottom))
    return [fitz.Rect(left / scale_x, top / scale_y, right / scale_x, bottom / scale_y)
            for left, top, right, bottom in sorted(set(proposed))]
