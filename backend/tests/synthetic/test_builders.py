# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke tests for the acceptance-test projects built through the services.

They check that COLL-MULTI / COLL-SINGLE / COLL-CONFIG come out of the builders
exactly as the test kit describes them: lifecycle states and members, page
structure, option permutation, answer keys, layout, PDF export and scan
readiness. Skipped without TEST_DATABASE_URL, like the rest of the integration
suite.
"""
from __future__ import annotations

import itertools
import time

import fitz
import pytest

from app.models.enums import (
    FormProjectStatus,
    FormVariantStatus,
    PageObjectRole,
    ProjectRole,
)
from app.models.user import User
from app.repositories import (
    form_project_member_repo,
    form_project_repo,
    form_variant_page_repo,
    page_object_repo,
)
from app.services import form_project_service
from app.services.form_variant_export_service import ExportMode, export_pdf
from app.utils import alignment_marks, code_rendering
from tests.synthetic.builders import (
    CollProject,
    build_coll_config,
    build_coll_multi,
    build_coll_single,
)
from tests.synthetic.specs import PAGE_H, PAGE_W, MatrixSpec

ALL_PROJECTS = ("coll_multi", "coll_single", "coll_config")


def _owner(db, project: CollProject) -> User:
    return db.get(User, project.owner_id)


def _labels_by_page(variant) -> dict[int, set[str]]:
    pages: dict[int, set[str]] = {}
    for refs in variant.objects.values():
        for ref in refs:
            pages.setdefault(ref.page_number, set()).add(ref.label)
    return pages


# ---------------------------------------------------------------------------
# 1. States and members
# ---------------------------------------------------------------------------


def _assert_members(db, project: CollProject) -> None:
    members = {
        m.user_id: m.role
        for m, _ in form_project_member_repo.list_for_project(db, project.project_id)
    }
    assert members == {
        project.owner_id: ProjectRole.OWNER,
        project.collaborator_id: ProjectRole.COLLABORATOR,
    }


def test_states_and_members(db, coll_multi, coll_single, coll_config):
    for project in (coll_multi, coll_single, coll_config):
        assert (
            form_project_repo.get(db, project.project_id).status
            is FormProjectStatus.ACTIVE
        )
        _assert_members(db, project)

    assert {k: v.status for k, v in coll_multi.variants.items()} == {
        "A": FormVariantStatus.PUBLISHED,
        "B": FormVariantStatus.PUBLISHED,
        "C": FormVariantStatus.PUBLISHED,
        "D": FormVariantStatus.DRAFT,
    }
    for project in (coll_single, coll_config):
        assert all(
            v.status is FormVariantStatus.PUBLISHED for v in project.variants.values()
        )
    assert set(coll_single.variants) == {"A"}
    assert set(coll_config.variants) == {"A", "B"}


# ---------------------------------------------------------------------------
# 2. COLL-MULTI structure per variant
# ---------------------------------------------------------------------------

_A_PAGES = {
    1: {"VID", "MATRICOLA", "NOME", "Q01", "Q02", "Q03", "Q04", "Q05", "Q06"},
    2: {"VID", "M-RIGHE", "M-COLONNE", "Q07", "Q08", "Q09", "BARCODE"},
    3: {"VID", "M-MULTI", "Q10", "Q11", "Q12", "APERTA"},
}
_EXPECTED_PAGES = {
    "A": _A_PAGES,
    "B": {
        1: _A_PAGES[1],
        2: _A_PAGES[2] | {"Q10", "Q11", "Q12"},
        3: {"VID", "M-MULTI", "APERTA"},
    },
    "C": {**_A_PAGES, 3: _A_PAGES[3] - {"Q12"}},
    "D": _A_PAGES,
}
_RESPONDENT_ID_COLS = {"A": 6, "B": 6, "C": 5, "D": 6}


def test_multi_structure(db, coll_multi):
    for key, variant in coll_multi.variants.items():
        pages = form_variant_page_repo.list_for_variant(db, variant.variant_id)
        assert [p.page_index for p in pages] == [1, 2, 3]
        assert variant.page_ids == tuple(p.id for p in pages)

        assert _labels_by_page(variant) == _EXPECTED_PAGES[key], key

        respondent_id = page_object_repo.get_with_configs(
            db, variant.obj("MATRICOLA").object_id
        )
        assert respondent_id.matrix_config.cols_count == _RESPONDENT_ID_COLS[key]
        assert len(respondent_id.matrix_config.cols) == _RESPONDENT_ID_COLS[key]
        assert respondent_id.role is PageObjectRole.RESPONDENT_IDENTIFIER

        barcode = page_object_repo.get(db, variant.obj("BARCODE").object_id)
        assert barcode.role is None

        vids = [page_object_repo.get_with_configs(db, r.object_id) for r in variant.objs("VID")]
        assert [r.page_number for r in variant.objs("VID")] == [1, 2, 3]
        assert vids[0].role is PageObjectRole.VARIANT_IDENTIFIER
        assert all(v.role is None for v in vids[1:])  # the binding sits on one row
        assert all(v.code_config.include_page_index for v in vids)
        assert all(v.shared_across_pages for v in vids)
        assert vids[0].shared_group_id is not None
        assert {v.shared_group_id for v in vids} == {vids[0].shared_group_id}
        assert {v.code_config.value for v in vids} == {variant.vid_value}

    values = [v.vid_value for v in coll_multi.variants.values()]
    assert len(set(values)) == len(values) == 4


# ---------------------------------------------------------------------------
# 3. Options of B are a permutation of A's
# ---------------------------------------------------------------------------


def test_multi_b_rotates_options(coll_multi):
    a, b = coll_multi.variants["A"], coll_multi.variants["B"]
    questions = [
        label for label, refs in a.objects.items() if refs[0].options and label in b.objects
    ]
    assert len(questions) == 12
    for label in questions:
        a_opts, b_opts = a.obj(label).options, b.obj(label).options
        assert set(a_opts) == set(b_opts)
        for text, a_opt in a_opts.items():
            b_opt = b_opts[text]
            assert b_opt.letter != a_opt.letter, (label, text)
            assert "ABCD".index(b_opt.letter) == ("ABCD".index(a_opt.letter) + 1) % 4
            assert (b_opt.is_correct, b_opt.score) == (a_opt.is_correct, a_opt.score)


# ---------------------------------------------------------------------------
# 4. Answer keys
# ---------------------------------------------------------------------------


def test_multi_scores(db, coll_multi):
    for key, variant in coll_multi.variants.items():
        q09 = variant.obj("Q09").options
        assert [o.score for o in q09.values() if o.score == -1.0] == [-1.0]
        assert q09["Opzione 4"].score == -1.0 and not q09["Opzione 4"].is_correct
        for label in ("Q05", "Q06"):
            assert sum(o.is_correct for o in variant.obj(label).options.values()) == 2

        spec = coll_multi.spec.variant(key)
        for label in ("M-RIGHE", "M-COLONNE", "M-MULTI"):
            mspec = spec.obj(label)
            assert isinstance(mspec, MatrixSpec)
            cfg = page_object_repo.get_with_configs(
                db, variant.obj(label).object_id
            ).matrix_config
            cells = {(c.row_index, c.col_index): (c.is_correct, c.score) for c in cfg.cells}
            assert cells == {rc: (True, 1.0) for rc in mspec.correct_cells}, label
    # The spec itself encodes the test kit's answer keys.
    a = coll_multi.spec.variant("A")
    assert set(a.obj("M-RIGHE").correct_cells) == {(r, r) for r in range(4)}
    assert set(a.obj("M-COLONNE").correct_cells) == {(r, 3 - r) for r in range(4)}
    assert set(a.obj("M-MULTI").correct_cells) == {
        (0, 0), (0, 2), (1, 1), (1, 3), (2, 0), (2, 3)
    }


# ---------------------------------------------------------------------------
# 5. Layout: inside the page, no overlaps (geometry read back from the DB)
# ---------------------------------------------------------------------------


def _overlap(a, b) -> bool:
    return (
        a.x < b.x + b.width and b.x < a.x + a.width
        and a.y < b.y + b.height and b.y < a.y + a.height
    )


@pytest.mark.parametrize("fixture", ALL_PROJECTS)
def test_layout_fits_and_does_not_overlap(db, request, fixture):
    project: CollProject = request.getfixturevalue(fixture)
    for variant in project.variants.values():
        for page_id in variant.page_ids:
            objs = page_object_repo.list_for_page(db, page_id)
            assert objs
            for o in objs:
                # Inside the page and outside the reserved bands.
                assert not alignment_marks.in_reserved_band(
                    o.x, o.y, o.width, o.height, PAGE_W, PAGE_H
                ), o.label
            for a, b in itertools.combinations(objs, 2):
                assert not _overlap(a, b), (variant.key, a.label, b.label)


# ---------------------------------------------------------------------------
# 6. Export: real code images, tagged payloads
# ---------------------------------------------------------------------------


def _spy_render_code(monkeypatch) -> list[dict]:
    """Wrap render_code: record arguments + return value, then call through."""
    calls: list[dict] = []
    original = code_rendering.render_code

    def spy(renderer, x, y, w, h, **kwargs):
        result = original(renderer, x, y, w, h, **kwargs)
        calls.append(
            {
                **kwargs,
                "payload": code_rendering.build_tagged_payload(
                    kwargs.get("role"), kwargs["value"], kwargs.get("page_index")
                ),
                "result": result,
            }
        )
        return result

    monkeypatch.setattr(code_rendering, "render_code", spy)
    return calls


@pytest.mark.parametrize("fixture", ALL_PROJECTS)
def test_export_pdf(db, request, monkeypatch, fixture):
    project: CollProject = request.getfixturevalue(fixture)
    owner = _owner(db, project)
    for variant in project.variants.values():
        calls = _spy_render_code(monkeypatch)
        pdf_bytes, _ = export_pdf(db, variant.variant_id, owner, ExportMode.STUDENT)
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            assert doc.page_count == project.spec.variant(variant.key).page_count

        assert all(c["result"] is True for c in calls), calls
        if variant.vid_value is None:
            assert calls == []
            continue
        vid_payloads = [c["payload"] for c in calls if c["value"] == variant.vid_value]
        assert vid_payloads == [
            code_rendering.build_tagged_payload(
                PageObjectRole.VARIANT_IDENTIFIER, variant.vid_value, n
            )
            for n in (1, 2, 3)
        ]
        barcode_payloads = [c["payload"] for c in calls if c["value"] == "LOT-0001"]
        assert barcode_payloads == ["LOT-0001"]
        assert len(calls) == 4


# ---------------------------------------------------------------------------
# 7. Scan readiness
# ---------------------------------------------------------------------------


def test_scan_readiness(db, coll_multi, coll_single, coll_config):
    multi = form_project_service.get_scan_readiness(
        db, coll_multi.project_id, _owner(db, coll_multi)
    )
    assert multi.ready is True
    assert multi.published_variant_count == 3
    assert multi.variant_identifier_variant_id in {
        coll_multi.variants[k].variant_id for k in "ABC"
    }

    single = form_project_service.get_scan_readiness(
        db, coll_single.project_id, _owner(db, coll_single)
    )
    assert single.ready is True
    assert single.published_variant_count == 1

    config = form_project_service.get_scan_readiness(
        db, coll_config.project_id, _owner(db, coll_config)
    )
    assert config.ready is False
    assert config.published_variant_count == 2


# ---------------------------------------------------------------------------
# 8. Build times (visible with pytest -s)
# ---------------------------------------------------------------------------


def test_build_times(db):
    for name, build in (
        ("COLL-MULTI", build_coll_multi),
        ("COLL-SINGLE", build_coll_single),
        ("COLL-CONFIG", build_coll_config),
    ):
        start = time.perf_counter()
        build(db)
        print(f"\n[synthetic] {name} built in {time.perf_counter() - start:.3f}s")
