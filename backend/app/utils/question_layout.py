# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""QUESTION layout geometry, in the pixel space of a FormVariantPage.

These five numbers decide WHERE the answer bubbles of a QUESTION object are.
The export prints them and extraction needs exactly the SAME numbers, because
it has to look for a blackened circle precisely where the export printed an
empty one. Two copies of these constants would be a silent, slow-moving bug:
the day one side moved a bubble by 2px the other would keep sampling the old
spot and quietly read blank sheets as answered.

So they live here, in ONE place, imported by both sides. The values are frozen
by ``tests/test_export_layout_freeze.py`` and mirrored on the authoring canvas
by ``frontend/src/utils/exportLayout.ts`` — see that test's header before
changing any of them.

Coordinates are relative to the QUESTION PageObject's own (x, y), in the same
pixel space as ``PageObject.x/y/width/height`` and
``FormVariantPage.width_px/height_px`` (see ``app.utils.pdf_rendering``).
Layout of one QUESTION, top to bottom::

    obj.y      ┌───────────────────────────────┐  ┐
               │ question_text (bold, 12px)    │  │ HEADER_H
    +HEADER_H  ├───────────────────────────────┤  ┘
               │  (o)   option_text            │  ┐ OPTION_H
               ├───────────────────────────────┤  ┘
               │  (o)   option_text            │
               └───────────────────────────────┘
                  ↑     ↑
                  │     └─ TEXT_X   (from obj.x)
                  └─ CIRCLE_CX      (from obj.x), radius CIRCLE_R

Options are laid out in ``order_index`` order, one OPTION_H band each, and the
rendering CLIPS: a band whose bottom would fall past ``obj.y + obj.height`` is
not drawn at all. Extraction has to reproduce that clipping too — an option the
export never printed has no bubble to read.
"""
from __future__ import annotations

# Height of the question-text band above the first option.
HEADER_H = 18
# Height of one option band.
OPTION_H = 18
# Radius of an option bubble.
CIRCLE_R = 5
# Horizontal centre of an option bubble, measured from the object's x.
CIRCLE_CX = 9
# Left edge of the option label text, measured from the object's x.
TEXT_X = 22
