# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Review actions and the candidate on real readings.

One batch with F04, F05, F09, F10, F11, F14 and the "Review anyway"
submission (page 2 image deleted before the run, so page 2 is placeholders),
through the real path; then the owner reviews them:

* F04: CONFIRM keeps the reading; SUBSTITUTE of a question, a matrix and a
  free text writes the reviewed value with the validity recomputed by the
  server; FLAG with a note; UNDO brings a reading back to pending; UNDO of
  an unresolved reading is refused (NOT_RESOLVED);
* F05: SUBSTITUTE with no option is "did not answer", not a FLAG;
* F09: CONFIRM of the ambiguous respondent ID sets the candidate "100009";
* F10, F11: an incomplete respondent ID is not confirmable; F10's SUBSTITUTE
  sets the candidate, F11's FLAG does not, then a manual edit does;
* F14: an undecoded barcode is not confirmable; SUBSTITUTE resolves it;
* placeholders: not confirmable (NO_READING); SUBSTITUTE resolves them.

"Accept all remaining" on its own batch: R01 (not a kit case: F04's marks,
F09's ambiguous respondent ID, F14's covered barcode), the "Review anyway"
submission and a respondent ID marked LIGHT — complete but below the
threshold, the one kit-like path where the bulk action sets the candidate.
It confirms exactly the bulk-acceptable unresolved readings in one save (one
version bump, one audit event), never a reading already ruled on, and leaves
free texts, the ambiguous respondent ID, the undecoded barcode and
placeholders to review one by one.

Closing at the end of the first flow: F04 with two readings left unresolved
— one of them brought back with UNDO, which counts as unresolved like a
reading never ruled on —, the "Review anyway" submission with its
placeholders never reviewed, and F09 once nothing is pending (what the
queue's "Ready to close" holds). Each becomes REVIEWED with its lock
released, the unresolved readings untouched and counted in the audit event;
the queue keeps that count on the closed submission while the batch list
leaves it out; a review save afterwards is refused. Then F04 is reopened:
its rulings kept, the two unresolved readings pending again; resolved, it
closes once more with nothing left.

The score: in the first flow each submission's score is declared like its
pending count and checked after every step, worked out by hand from the
answer key (an unresolved reading scores 0). Closing writes ``total_score``
equal to the last score shown, reopening clears it, and F04 closes again
with a new value: after the reopening M-COLONNE gets its key by SUBSTITUTE
(27 instead of 23).

After every step the queue and the review page agree on the submission's
pending count and candidate, and the project's batch list shows the sum over
the submissions still open; the expected values are declared here. Each
refusal writes nothing. Marked ``synthetic``:
``pytest -m synthetic tests/synthetic -s``.
"""
from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.enums import ReviewAction, ReviewOutcome, SubmissionStatus, Validity
from app.models.submission_review import SubmissionReview
from app.schemas.submission import ReviewActionType, SubmissionReviewActionRequest
from app.services import (
    audit_service,
    review_action_service,
    review_anyway_service,
    review_rules,
    submission_batch_service,
    submission_close_service,
    submission_lock_service,
    submission_review_service,
)
from tests.synthetic.generator import MarkStyle, SheetOptions, digits
from tests.synthetic.runner import queue_state, review_objects, upload_scenarios
from tests.synthetic.scenarios import (
    MAX_A,
    PAGE_2_LABELS,
    REVIEW_ANYWAY,
    SCENARIOS,
    clean_a,
)

pytestmark = [
    pytest.mark.synthetic,
    pytest.mark.skipif(
        shutil.which("tesseract") is None,
        reason="Tesseract not installed: the scenarios use real OCR",
    ),
]

CONFIRM = ReviewActionType.CONFIRM
SUBSTITUTE = ReviewActionType.SUBSTITUTE
FLAG = ReviewActionType.FLAG
UNDO = ReviewActionType.UNDO
IDS = ("F04", "F05", "F09", "F10", "F11", "F14")
ALWAYS_REVIEW = submission_review_service.ALWAYS_REVIEW
AMBIGUOUS_DIGITS = submission_review_service.AMBIGUOUS_DIGITS
DECODE_FAILED = submission_review_service.DECODE_FAILED
EXTRACTION_FAILED = submission_review_service.EXTRACTION_FAILED
INVALID_ANSWER = submission_review_service.INVALID_ANSWER
LOW_CONFIDENCE = submission_review_service.LOW_CONFIDENCE

# Not kit cases. R01: F04's marks with F09's ambiguous column and F14's
# covered barcode, so one submission holds every kind of reading the bulk
# action must take or leave.
R01 = replace(
    SCENARIOS["F04"],
    id="R01",
    description="Accept all remaining — F04's marks, ambiguous respondent ID, BARCODE covered",
    fill=replace(
        SCENARIOS["F04"].fill,
        cells={
            **SCENARIOS["F04"].fill.cells,
            "MATRICOLA": digits("100", ("0", "3"), "01"),
        },
    ),
    options=SheetOptions(seed=101, cover=(("BARCODE", (2,)),)),
)
# A complete respondent ID marked LIGHT: read 0.606–0.645 on seeds 11–15,
# inside LIGHT's 0.50–0.70 band — selected, under the 0.7 threshold.
LIGHT_CELLS = digits("100031")
LIGHT = replace(
    SCENARIOS["F01"],
    id="L01",
    description="Accept all remaining — complete respondent ID in LIGHT marks, below threshold",
    fill=replace(
        clean_a("100031"),
        styles={("MATRICOLA", cell): MarkStyle.LIGHT for cell in LIGHT_CELLS},
    ),
    options=SheetOptions(seed=13),
)


class _Review:
    """The owner reviewing one batch: acts by label, tracks each submission's
    version, and checks the queue after every step against declared values.
    With *scores*, the score of each submission too."""

    def __init__(
        self, db, clock, batch, subs, owner, pending, candidates, scores=None
    ) -> None:
        self.db, self.clock, self.batch, self.subs, self.owner = db, clock, batch, subs, owner
        self.pending = dict(pending)  # declared, per submission
        self.candidates = dict(candidates)  # declared, per submission
        self.scores = dict(scores) if scores is not None else None  # raw score, declared
        self.versions = {key: 0 for key in subs}
        self.closed: set[str] = set()

    def detail(self, key):
        return submission_review_service.get_review_detail(self.db, self.subs[key].id, self.owner)

    def obj(self, key, label):
        return review_objects(self.detail(key))[label]

    def act(self, key, label, action, *, value=None, note=None):
        """One admitted action; each one a second later, so the log is ordered."""
        self.clock.advance(seconds=1)
        detail = review_action_service.apply_action(
            self.db,
            self.subs[key].id,
            self.obj(key, label).result_id,
            self.owner,
            SubmissionReviewActionRequest(
                action=action, review_version=self.versions[key], note=note, value=value
            ),
        )
        self.versions[key] += 1
        assert detail.review_version == self.versions[key]
        return review_objects(detail)[label]

    def refused(self, key, label, action, *, value=None) -> dict:
        """One action expected to be refused with a 409; rolled back as
        ``get_db`` would. Nothing is written: same version, same ruling."""
        before = self.obj(key, label)
        self.db.commit()
        with pytest.raises(HTTPException) as excinfo:
            review_action_service.apply_action(
                self.db,
                self.subs[key].id,
                before.result_id,
                self.owner,
                SubmissionReviewActionRequest(
                    action=action, review_version=self.versions[key], value=value
                ),
            )
        self.db.rollback()
        assert excinfo.value.status_code == 409, excinfo.value.detail
        assert self.detail(key).review_version == self.versions[key]
        assert self.obj(key, label) == before
        return excinfo.value.detail

    def accept(self, key):
        """"Accept all remaining": one save, one version bump."""
        accepted = review_action_service.accept_remaining(
            self.db, self.subs[key].id, self.owner, self.versions[key]
        )
        self.versions[key] += 1
        assert accepted.review_version == self.versions[key]
        return accepted

    def close(self, key):
        """"Close submission": one save, one version bump. With scores,
        ``total_score`` is the last score shown."""
        shown = self.detail(key).score if self.scores is not None else None
        detail = submission_close_service.close_submission(
            self.db, self.subs[key].id, self.owner, self.versions[key]
        )
        self.versions[key] += 1
        self.closed.add(key)
        assert detail.review_version == self.versions[key]
        assert detail.status is SubmissionStatus.REVIEWED and detail.lock is None
        if self.scores is not None:
            assert shown is not None, key
            assert self.total_score(key) == shown.raw_score == self.scores[key], (
                key, self.total_score(key), shown.raw_score, self.scores[key]
            )
        return detail

    def reopen(self, key):
        """"Reopen submission": one version bump, the lock ours."""
        detail = submission_close_service.reopen_submission(
            self.db, self.subs[key].id, self.owner, self.versions[key]
        )
        self.versions[key] += 1
        self.closed.discard(key)
        assert detail.review_version == self.versions[key]
        assert detail.status is SubmissionStatus.SCANNED
        assert detail.lock is not None and detail.lock.user_id == self.owner.id
        if self.scores is not None:
            assert self.total_score(key) is None, (key, self.total_score(key))
        return detail

    def total_score(self, key) -> float | None:
        """``Submission.total_score``, as persisted."""
        self.db.refresh(self.subs[key])
        return self.subs[key].total_score

    def edit_candidate(self, key, value):
        state = submission_review_service.edit_candidate(
            self.db, self.subs[key].id, self.owner, value, self.versions[key]
        )
        self.versions[key] += 1
        assert state.review_version == self.versions[key]

    def expect(self, key, *, pending=None, candidate=..., score=None) -> None:
        """Update the declared values of *key*, then check the queue (and,
        with scores, the score of the review read)."""
        if pending is not None:
            self.pending[key] = pending
        if candidate is not ...:
            self.candidates[key] = candidate
        if score is not None:
            assert self.scores is not None, "score declared on a flow without scores"
            self.scores[key] = score
        if self.scores is not None:
            shown = self.detail(key).score
            assert shown is not None, key
            assert shown.raw_score == self.scores[key], (key, shown.raw_score, self.scores[key])
        state = queue_state(self.db, self.batch, self.subs[key], self.owner)
        want = SubmissionStatus.REVIEWED if key in self.closed else SubmissionStatus.SCANNED
        assert state.status is want, (key, state)
        # On a closed submission: the readings left unresolved.
        assert (state.pending, state.review_pending) == (self.pending[key],) * 2, (key, state)
        open_pending = sum(n for k, n in self.pending.items() if k not in self.closed)
        assert state.batch_pending == open_pending, (key, state)
        assert state.candidate == self.candidates[key], (key, state)

    def log(self, key, label) -> list[ReviewAction]:
        result_id = self.obj(key, label).result_id
        return list(
            self.db.scalars(
                select(SubmissionReview.action)
                .where(SubmissionReview.submission_object_result_id == result_id)
                .order_by(SubmissionReview.created_at)
            ).all()
        )


def _run_with_placeholders(db, batch, subs, owner) -> None:
    """Run the batch with the "Review anyway" submission's page 2 image gone,
    then "Review anyway": page 2's objects become placeholders."""
    page_2 = next(p for p in subs["RA"].pages if p.form_variant_page.page_index == 2)
    Path(page_2.source_file_path).unlink()
    summary = submission_batch_service.run_batch_extraction(db, batch.id, owner)
    assert summary.errors == len(PAGE_2_LABELS), summary.error_details
    review_anyway_service.review_anyway(db, subs["RA"].id, owner)


def _not_confirmable(review, key, label, reason) -> None:
    assert not review.obj(key, label).confirmable
    refusal = review.refused(key, label, CONFIRM)
    assert refusal["code"] == review_action_service.NOT_CONFIRMABLE
    assert refusal["reason"] == reason


def test_review_actions_on_real_readings(db, scan_storage, clock, coll_multi):
    scenarios = [SCENARIOS[i] for i in IDS] + [REVIEW_ANYWAY]
    owner, batch, subs = upload_scenarios(db, coll_multi, "Actions", scenarios)
    _run_with_placeholders(db, batch, subs, owner)

    # Declared from the scenarios: each submission's review, and its candidate
    # as read; RA's is F01's review with page 2 as placeholders.
    review = _Review(
        db, clock, batch, subs, owner,
        pending={
            **{i: len(SCENARIOS[i].expected.review) for i in IDS},
            "RA": len(PAGE_2_LABELS) + 2,
        },
        candidates={**{i: SCENARIOS[i].expected.candidate for i in IDS}, "RA": "100012"},
        # From the scenarios; RA is F01's 28 with page 2 as placeholders,
        # pending: M-RIGHE, M-COLONNE, Q07–Q09 score 0 (−4 −4 −3).
        scores={**{i: SCENARIOS[i].expected.score.points for i in IDS}, "RA": 17},
    )
    for key in subs:
        assert review.detail(key).score.max_possible_score == MAX_A, key
        review.expect(key)
    a = coll_multi.variants["A"]

    # --- F04: CONFIRM, SUBSTITUTE per type, FLAG, UNDO -----------------------
    q01 = review.act("F04", "Q01", CONFIRM)
    assert q01.review_outcome is ReviewOutcome.CONFIRMED and q01.reviewed_payload is None
    assert q01.detected_payload["validity"] == Validity.TOO_MANY_ANSWERS.value
    # TOO_MANY_ANSWERS confirmed: min(0, 0 + 1) = 0, as when pending.
    review.expect("F04", pending=5, score=18)

    key_option = a.obj("Q02").options["Opzione 3"].option_id  # Q02's key
    q02 = review.act("F04", "Q02", SUBSTITUTE, value={"selected_option_ids": [str(key_option)]})
    assert q02.review_outcome is ReviewOutcome.SUBSTITUTED
    assert q02.reviewed_payload == {
        "selected_option_ids": [str(key_option)],
        "validity": Validity.VALID.value,
    }
    review.expect("F04", pending=4, score=19)

    diagonal = [[r, r] for r in range(4)]  # M-RIGHE's key: one per row
    m_righe = review.act("F04", "M-RIGHE", SUBSTITUTE, value={"selected_cells": diagonal})
    assert m_righe.reviewed_payload["rows"] == [
        {"row_index": r, "selected_col_indices": [r], "validity": Validity.VALID.value}
        for r in range(4)
    ]
    assert m_righe.reviewed_payload["cols"] == []
    review.expect("F04", pending=3, score=23)

    name = review.act("F04", "NOME", FLAG, note="Illegible")
    assert name.review_outcome is ReviewOutcome.FLAGGED and name.review_note == "Illegible"
    open_text = review.act("F04", "APERTA", SUBSTITUTE, value={"text": "  Transcribed answer  "})
    assert open_text.reviewed_payload == {"text": "Transcribed answer"}
    review.expect("F04", pending=1)

    undone = review.act("F04", "Q01", UNDO)
    assert not undone.resolved and undone.review_outcome is None and undone.reviewed_by is None
    assert review.log("F04", "Q01") == [ReviewAction.CONFIRMED, ReviewAction.UNDONE]
    review.expect("F04", pending=2)

    # UNDO of a reading never ruled on: refused.
    refusal = review.refused("F04", "M-COLONNE", UNDO)
    assert refusal["code"] == review_action_service.NOT_RESOLVED
    review.expect("F04")

    # --- F05: "did not answer" is not a FLAG ---------------------------------
    q03 = review.act("F05", "Q03", SUBSTITUTE, value={"selected_option_ids": []})
    assert q03.review_outcome is ReviewOutcome.SUBSTITUTED
    assert q03.reviewed_payload == {"selected_option_ids": [], "validity": Validity.NO_ANSWER.value}
    q04 = review.act("F05", "Q04", FLAG)
    assert q04.review_outcome is ReviewOutcome.FLAGGED and q04.reviewed_payload is None
    review.expect("F05", pending=3)

    # --- F09: CONFIRM of the ambiguous respondent ID sets the candidate ------
    respondent_id = review.obj("F09", "MATRICOLA")
    assert respondent_id.confirmable and respondent_id.sets_candidate
    review.act("F09", "MATRICOLA", CONFIRM)
    review.expect("F09", pending=2, candidate="100009")

    # --- F10, F11: an incomplete respondent ID is not confirmable ------------
    _not_confirmable(review, "F10", "MATRICOLA", review_rules.INCOMPLETE_IDENTIFIER)
    substituted = review.act("F10", "MATRICOLA", SUBSTITUTE, value={"value": "100010"})
    assert substituted.reviewed_payload == {"value": "100010"}
    review.expect("F10", pending=2, candidate="100010")

    _not_confirmable(review, "F11", "MATRICOLA", review_rules.INCOMPLETE_IDENTIFIER)
    review.act("F11", "MATRICOLA", FLAG)
    review.expect("F11", pending=2, candidate=None)  # FLAG never sets it
    # The candidate corrected by hand ("No candidate" goes).
    review.edit_candidate("F11", "100011")
    review.expect("F11", candidate="100011")

    # --- F14: an undecoded barcode is not confirmable ------------------------
    _not_confirmable(review, "F14", "BARCODE", review_rules.NOT_DECODED)
    barcode = review.act("F14", "BARCODE", SUBSTITUTE, value={"value": "LOT-0001"})
    assert barcode.reviewed_payload == {"value": "LOT-0001"}
    review.expect("F14", pending=2)

    # --- Placeholders: nothing to confirm, the answer comes from paper -------
    placeholder = review.obj("RA", "Q07")
    assert placeholder.detected_payload is None
    _not_confirmable(review, "RA", "Q07", review_rules.NO_READING)
    key_option = a.obj("Q07").options["Opzione 1"].option_id  # Q07's key
    q07 = review.act("RA", "Q07", SUBSTITUTE, value={"selected_option_ids": [str(key_option)]})
    assert q07.reviewed_payload["validity"] == Validity.VALID.value
    review.expect("RA", pending=len(PAGE_2_LABELS) + 1, score=18)

    # --- Closing -------------------------------------------------------------
    # F04: Q01 brought back with UNDO and M-COLONNE never ruled on stay
    # unresolved, untouched, and are counted.
    left = {k for k, o in review_objects(review.detail("F04")).items() if not o.resolved}
    assert left == {"Q01", "M-COLONNE"}
    review.close("F04")
    assert {k for k, o in review_objects(review.detail("F04")).items() if not o.resolved} == left
    assert review.log("F04", "Q01") == [ReviewAction.CONFIRMED, ReviewAction.UNDONE]
    assert _closed_events(db, subs["F04"])[0].metadata_json == {
        "unresolved_count": 2, "review_version": review.versions["F04"],
    }
    review.expect("F04")
    # A review save on the closed submission is refused.
    refusal = review.refused("F04", "M-COLONNE", FLAG)
    assert refusal["code"] == submission_lock_service.NOT_REVIEWABLE

    # --- Reopening -----------------------------------------------------------
    # F04 reopened: every ruling kept, the two readings left unresolved are
    # pending again; once they are resolved it closes with nothing left.
    review.reopen("F04")
    assert {k for k, o in review_objects(review.detail("F04")).items() if not o.resolved} == left
    assert _reopened_events(db, subs["F04"])[0].metadata_json == {
        "unresolved_count": 2, "review_version": review.versions["F04"],
    }
    review.expect("F04")
    review.act("F04", "Q01", CONFIRM)
    review.expect("F04", pending=1, score=23)
    # M-COLONNE's key, one per column: a new value for the second closing.
    anti_diagonal = [[r, 3 - r] for r in range(4)]
    m_colonne = review.act(
        "F04", "M-COLONNE", SUBSTITUTE, value={"selected_cells": anti_diagonal}
    )
    # Grouped by column, like the extraction under ONE_PER_COLUMN.
    assert m_colonne.reviewed_payload["cols"] == [
        {"col_index": c, "selected_row_indices": [3 - c], "validity": Validity.VALID.value}
        for c in range(4)
    ]
    assert m_colonne.reviewed_payload["rows"] == []
    review.expect("F04", pending=0, score=27)
    review.close("F04")
    assert len(_closed_events(db, subs["F04"])) == 2
    review.expect("F04")

    # RA: the placeholders nobody reviewed are left unresolved like any reading.
    review.close("RA")
    assert _closed_events(db, subs["RA"])[0].metadata_json["unresolved_count"] == (
        len(PAGE_2_LABELS) + 1
    )
    review.expect("RA")

    # F09: nothing pending once its free texts are flagged — "Ready to close".
    for label in ("NOME", "APERTA"):
        review.act("F09", label, FLAG)
    review.expect("F09", pending=0)
    review.close("F09")
    assert _closed_events(db, subs["F09"])[0].metadata_json["unresolved_count"] == 0
    review.expect("F09")

    # Every submission once more: nothing moved behind another's back.
    for key in subs:
        review.expect(key)
    print(
        f"\n[synthetic] actions: pending {review.pending}, versions {review.versions}, "
        f"candidates {review.candidates}, closed {sorted(review.closed)}"
    )


def _closed_events(db, submission) -> list[AuditLog]:
    return list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.action == audit_service.SUBMISSION_CLOSED,
                AuditLog.entity_id == str(submission.id),
            )
        ).all()
    )


def _reopened_events(db, submission) -> list[AuditLog]:
    return list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.action == audit_service.SUBMISSION_REOPENED,
                AuditLog.entity_id == str(submission.id),
            )
        ).all()
    )


def _accepted_events(db, submission) -> list[AuditLog]:
    return list(
        db.scalars(
            select(AuditLog).where(
                AuditLog.action == audit_service.SUBMISSION_ACCEPTED_REMAINING,
                AuditLog.entity_id == str(submission.id),
            )
        ).all()
    )


def test_accept_all_remaining(db, scan_storage, clock, coll_multi):
    owner, batch, subs = upload_scenarios(
        db, coll_multi, "Accept all remaining", [R01, REVIEW_ANYWAY, LIGHT]
    )
    _run_with_placeholders(db, batch, subs, owner)

    # The premises, declared: what is in review, with why, and what the bulk
    # action may take.
    in_review = {
        "R01": {
            "Q01": {INVALID_ANSWER}, "Q02": {INVALID_ANSWER},
            "M-RIGHE": {INVALID_ANSWER}, "M-COLONNE": {INVALID_ANSWER},
            "MATRICOLA": {AMBIGUOUS_DIGITS}, "BARCODE": {DECODE_FAILED},
            "NOME": {ALWAYS_REVIEW}, "APERTA": {ALWAYS_REVIEW},
        },
        "RA": {
            **{label: {EXTRACTION_FAILED} for label in PAGE_2_LABELS},
            "NOME": {ALWAYS_REVIEW}, "APERTA": {ALWAYS_REVIEW},
        },
        "L01": {
            "MATRICOLA": {LOW_CONFIDENCE},
            "NOME": {ALWAYS_REVIEW}, "APERTA": {ALWAYS_REVIEW},
        },
    }
    bulk = {"R01": {"Q01", "Q02", "M-RIGHE", "M-COLONNE"}, "RA": set(), "L01": {"MATRICOLA"}}
    review = _Review(
        db, clock, batch, subs, owner,
        pending={key: len(objs) for key, objs in in_review.items()},
        candidates={"R01": None, "RA": "100012", "L01": None},
    )
    for key, objs in in_review.items():
        listed = review_objects(review.detail(key))
        assert {k: set(o.flag_reasons) for k, o in listed.items()} == objs, key
        assert {k for k, o in listed.items() if o.bulk_acceptable} == bulk[key], key
        review.expect(key)

    # R01: a reading already ruled on is never overwritten. Q01 flagged first.
    review.act("R01", "Q01", FLAG)
    review.expect("R01", pending=7)
    accepted = review.accept("R01")
    assert accepted.accepted_count == 3
    after = review_objects(accepted)
    assert after["Q01"].review_outcome is ReviewOutcome.FLAGGED
    for label in ("Q02", "M-RIGHE", "M-COLONNE"):
        assert after[label].review_outcome is ReviewOutcome.CONFIRMED, label
        assert after[label].reviewed_payload is None
    left = {"MATRICOLA", "BARCODE", "NOME", "APERTA"}
    assert {k for k, o in after.items() if not o.resolved} == left
    (event,) = _accepted_events(db, subs["R01"])
    assert event.metadata_json == {"accepted_count": 3}
    review.expect("R01", pending=len(left), candidate=None)  # the ambiguous one is left

    # Placeholders: nothing to take; the save still counts as one.
    accepted = review.accept("RA")
    assert accepted.accepted_count == 0
    assert not any(o.resolved for o in review_objects(accepted).values())
    review.expect("RA")

    # A complete respondent ID under the threshold is taken, and sets the candidate.
    assert review.obj("L01", "MATRICOLA").sets_candidate
    accepted = review.accept("L01")
    assert accepted.accepted_count == 1
    assert review_objects(accepted)["MATRICOLA"].review_outcome is ReviewOutcome.CONFIRMED
    review.expect("L01", pending=2, candidate="100031")

    for key in subs:
        review.expect(key)
    print(
        f"\n[synthetic] accept all remaining: pending {review.pending}, "
        f"versions {review.versions}, candidates {review.candidates}"
    )
