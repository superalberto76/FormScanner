# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading ONE bubble off a scan: the one thing that touches pixels.

Shared by QUESTION and MATRIX extraction, which need exactly the same
measurement on exactly the same kind of mark. A MATRIX cell and a QUESTION
option are the same circle printed by the same exporter at the same radius, so
reading them with two copies of this function would be two chances to
disagree about what "blackened" means — and the disagreement would show up as
a whole object type silently reading blank.

Nothing here knows about PageObjects, sessions or storage: it takes a greyscale
image and a circle, and returns a number. What that number MEANS — a selection,
a confidence, a validity — is decided by the extractor that asked.

The two thresholds live here for the same reason the function does: they are
the definition of "ink" and of "filled in", and every extractor has to use the
same one.
"""
from __future__ import annotations

from PIL import Image

# A pixel counts as INK below this 0-255 grey level. Fixed, global, and
# deliberately not adaptive: adaptive thresholding (Otsu, per-window means)
# only earns its complexity once real scans show it is needed. 128 is the
# midpoint — pencil and pen on white paper sit far below it.
DARK_PIXEL_THRESHOLD = 128

# Fraction of dark pixels in a bubble's sampling window above which the option
# is read as SELECTED. Half the window is a lot of ink for a bubble that was
# only outlined (an empty circle's stroke covers a small fraction of it), and
# comfortably little for one that was filled in.
SELECTION_FILL_THRESHOLD = 0.5


def sample_circle_fill_ratio(
    gray_image: Image.Image, cx: float, cy: float, radius: float
) -> float:
    """Fraction of pixels below :data:`DARK_PIXEL_THRESHOLD` inside the bubble.

    The sampled area is the DISC of the given *radius* centred on the point:
    the square crop below is only how the candidate pixels are fetched, and a
    pixel counts — as ink or as paper — solely when its centre falls within the
    radius. So the measurement is over the bubble itself, and a fully blackened
    option reads ~1.0.

    That matters because the ratio IS the confidence reported for the reading.
    Sampling the circumscribed square instead would let the four corners, which
    are always paper, cap a perfectly filled bubble at π/4 ≈ 0.79 — leaving
    barely 0.085 of margin under the 0.7 default confidence threshold and
    sending unambiguous readings to review. The whole usable range of the axis
    is worth more than the handful of comparisons the mask costs; the crop is a
    few dozen pixels a side, so a plain double loop needs no numpy.

    Clipped to the image: a bubble falling partly off the sheet is sampled on
    the part that exists, and one falling entirely off returns 0.0 — "nothing
    was read here", which is the truthful reading of a bubble the scan does not
    contain.

    *gray_image* must already be in Pillow's ``L`` mode; the caller converts
    once per page rather than once per bubble.
    """
    width, height = gray_image.size
    left = max(int(round(cx - radius)), 0)
    top = max(int(round(cy - radius)), 0)
    right = min(int(round(cx + radius)), width)
    bottom = min(int(round(cy + radius)), height)
    if right <= left or bottom <= top:
        return 0.0

    pixels = list(gray_image.crop((left, top, right, bottom)).getdata())
    crop_width = right - left
    radius_sq = radius * radius

    sampled = 0
    dark = 0
    for index, value in enumerate(pixels):
        # A pixel is the unit SQUARE it covers, so its centre — not its
        # top-left corner — is what the disc test has to be applied to.
        dx = left + index % crop_width + 0.5 - cx
        dy = top + index // crop_width + 0.5 - cy
        if dx * dx + dy * dy > radius_sq:
            continue
        sampled += 1
        if value < DARK_PIXEL_THRESHOLD:
            dark += 1

    if sampled == 0:
        # A radius too small to contain the centre of any pixel. Nothing was
        # read, which is not the same as reading paper.
        return 0.0
    return dark / sampled
