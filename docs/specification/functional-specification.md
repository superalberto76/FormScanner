# FormScanner — Functional specification

This document describes what FormScanner does: its concepts, its workflow and the rules it applies, from the printed sheet to the exported results. It describes the product as it is.

## Contents

1. [Introduction](#1-introduction)
2. [Concepts](#2-concepts)
3. [Workflow overview](#3-workflow-overview)
4. [Authoring rules for scanning](#4-authoring-rules-for-scanning)
5. [Ingestion and matching](#5-ingestion-and-matching)
6. [Sheet alignment](#6-sheet-alignment)
7. [Extraction](#7-extraction)
8. [Review](#8-review)
9. [Scoring](#9-scoring)
10. [Results export](#10-results-export)
11. [Submission lifecycle](#11-submission-lifecycle)
12. [Known limitations](#12-known-limitations)
13. [Out of scope and possible extensions](#13-out-of-scope-and-possible-extensions)
- [Appendix: Glossary](#appendix-glossary)

## 1. Introduction

### 1.1 Purpose

FormScanner is a platform for paper-based exams and questionnaires. Authors design multiple-choice forms, export them as printable PDFs and print them. Respondents fill them in by hand. The completed sheets are scanned and uploaded; FormScanner identifies each sheet, reads the answers, sends the uncertain readings to a human reviewer, computes the score and exports the results.

### 1.2 Audience

- Users and administrators who want to know exactly how the product behaves.
- Contributors, who need the rules and the reasons behind them before changing the code.

### 1.3 Scope

This specification covers the whole **scanning pipeline** in detail: the authoring rules that make a form scannable, ingestion and matching of the scanned sheets, sheet alignment, extraction of the answers, review, scoring and results export.

Authoring in general (projects, variants, pages, object types, linked copies, PDF export) and user accounts are summarised in [chapter 2](#2-concepts) only as far as needed to understand the pipeline. Their detailed rules are not part of this document.

Technical details (API, data model, PDF layout constants, image processing, code payload formats) belong to the technical documentation.

### 1.4 Conventions

- `MATCH_FAILED`, `RESPONDENT_IDENTIFIER`, `FREE_TEXT_AREA`: codes used by the API for states, roles, object types and reasons.
- "Accept all remaining", "Take over": labels of the user interface, exactly as they appear in the application.
- **Rationale** notes explain why a rule is the way it is. They are part of the specification: a change that goes against a rationale changes the product, not just the code.
- Terms with a specific meaning (*submission*, *sheet*, *candidate*, *unresolved*…) are defined in the [Glossary](#appendix-glossary).

## 2. Concepts

This chapter describes the product model in summary. Only the parts that affect the scanning pipeline are specified in detail, in the following chapters.

### 2.1 Users and access

- Users sign in with email and password. There are two system roles: **USER** and **ADMIN**. Administrators create user accounts, enable and disable them, reset their passwords, see every project and read the whole audit log.
- Access to a project depends on **membership**. Its creator becomes **OWNER**; owners add other users as **COLLABORATOR**, promote and demote members, and remove them. A project always keeps at least one owner.
- Owners and collaborators share all the content work: authoring, publishing, exporting PDFs, scanning, review and results export. Only an owner (or an administrator) changes the project's status, manages members and deletes the project.
- All members see all the batches and submissions of their project: there are no personal queues.

### 2.2 Form project

A **form project** (`FormProject`) is one exam, survey or questionnaire: a name, a description, an event date and a type (exam, survey, questionnaire, generic form; the type is informative). Its lifecycle:

| Status | Meaning |
| --- | --- |
| `DRAFT` | The form is being designed. Variants can be created, edited, cloned and published. |
| `ACTIVE` | Sheets are being collected. Batches can be created and sheets uploaded. Variants can still be published, but not edited. |
| `CLOSED` | Collection is over. Existing batches remain available for extraction, review and export. |
| `ARCHIVED` | Final. Published variants become archived. |

Allowed transitions: `DRAFT` → `ACTIVE` (requires at least one published variant) or `CLOSED`; `ACTIVE` → `CLOSED` or `DRAFT`; `CLOSED` → `DRAFT` or `ARCHIVED`. A project can be deleted only while all its variants are drafts.

### 2.3 Variants

A **variant** (`FormVariant`) is one printable version of the form, for example versions A, B and C of an exam with the questions in a different order. A project may have several variants. Lifecycle:

- `DRAFT`: editable.
- `PUBLISHED`: read-only, printable and recognisable by scanning. Publishing is refused if the variant has no pages, if any object lies in the reserved bands (4.3), or if its variant identifier value is already used by another published variant of the project (4.2). A published variant is never edited: to change it, the user clones it ("Clone") into a new draft.
- `ARCHIVED`: set when the project is archived.

Only the published variants of a project take part in scanning.

### 2.4 Pages

A variant has one or more **pages** (`FormVariantPage`), A4 portrait, numbered from 1. Pages can be added, duplicated, reordered and removed while the variant is a draft.

### 2.5 Page objects

A page carries **objects** (`PageObject`) placed freely on it, each of one type:

| Type | Purpose |
| --- | --- |
| `TEXT` | Printed text (instructions, headings). |
| `IMAGE` | A picture (PNG, JPEG, BMP or SVG). |
| `CODE` | A QR code or a barcode (CODE128, CODE39, EAN13, EAN8, UPC-A) with a value. |
| `FREE_TEXT_AREA` | A box where the respondent writes by hand. |
| `QUESTION` | A single-choice or multiple-choice question with its options; each option has a bubble, may be marked correct and may have a score, also negative. |
| `MATRIX` | A grid of bubbles with labelled rows and columns, a selection rule (one or several per row, one or several per column, free) and, per cell, a correct flag and an optional score. A matrix marked "Evaluative" counts for the score. |

An object can be hidden: it stays in the variant but is not printed.

An object can carry one of two **scanning roles**, `VARIANT_IDENTIFIER` and `RESPONDENT_IDENTIFIER` (4.1).

### 2.6 Linked copies

An object can be **shown on all pages** ("Show on all pages"): it is then copied onto every page added afterwards, and the copies stay **linked**. Moving, resizing, editing or deleting one copy applies to all of them. "Unlink from other pages" detaches every copy, after which each page keeps an independent object; "Relink to all pages" links them again.

For scanning, linked copies are one object: a question shown on every page is one question for the score and one column in the export, read on its first copy (9, 10). A scanning role held by an object applies to all its linked copies (4.1).

### 2.7 Printed PDFs

Each variant is exported as two PDFs: **"student"**, with the answers blank, which is printed and filled in, and **"solution"**, with the correct options and cells filled in. Both have exactly the same layout, including the alignment marks (6.1), so a solution sheet can be laid over a student sheet. Codes with a scanning role are printed with their role tag (4.1).

### 2.8 Scanning entities

| Term | Entity | Meaning |
| --- | --- | --- |
| Batch | `SubmissionBatch` | A scan session of one project: a name, a description and a confidence threshold. |
| Submission | `Submission` | The sheets of one respondent on one variant: a candidate, a variant, a state, the missing pages. |
| Sheet | `SubmissionPage` | One scanned physical page, bound to a page of the variant. |
| Reading | `SubmissionObjectResult` | What extraction read for one object on one sheet, and what review decided about it. |

Batches are kept until their project is deleted.

The **candidate** of a submission is a raw identifier read from the sheet or typed by a reviewer. FormScanner has no register of respondents (7.6).

## 3. Workflow overview

1. **Design.** In a `DRAFT` project, the author composes one or more variants and, if needed, assigns the scanning roles: a variant code to tell the variants apart, a respondent identifier to tell the respondents apart (4).
2. **Publish and print.** The author publishes the variants and activates the project. The "student" PDFs are printed and filled in by the respondents.
3. **Upload.** A member creates a batch and uploads the scanned sheets, as PDFs or loose images (5.3). Each upload groups the sheets into submissions (5.5), reads the identification codes and matches every submission to its variant and its pages (5.6, 5.7). Submissions whose variant cannot be determined wait for a manual assignment (5.8).
4. **Extract.** A member runs the extraction of the batch (7.1). Every sheet is aligned to its template (6) and its objects are read; uncertain readings are marked for review (7.4).
5. **Review.** Reviewers work through the batch queue, open each submission, confirm or correct the readings in review and close the submission (8). The score is computed as they go (9).
6. **Export.** When every submission is closed or set aside as unresolvable, the batch results are exported as a summary and a detail file (10).

The states a submission goes through are described in chapter 11.

## 4. Authoring rules for scanning

The author composes a variant freely. The rules in this chapter are the only constraints scanning puts on the layout.

### 4.1 Scanning roles

An existing object can be given a special role ("Special role" in the object panel):

| Role | UI label | Allowed on | Purpose |
| --- | --- | --- | --- |
| `VARIANT_IDENTIFIER` | "Variant identifier" | `CODE` | Tells which variant a sheet belongs to (5.6). |
| `RESPONDENT_IDENTIFIER` | "Respondent identifier" | `CODE`, `MATRIX`, `FREE_TEXT_AREA` | Identifies, as raw data, the respondent: a pre-printed or stuck-on code, a respondent-ID grid where the respondent marks one digit per column, or a handwritten field (name, student number). |

- Both roles are **optional** and independent: a variant may have none, one or both, on different objects. A project with a single published variant needs no variant identifier; an anonymous questionnaire needs no respondent identifier.
- At most **one object per role per variant**, and at most one role per object. If, for example, name and surname are two separate fields, only one of them can be the respondent identifier; the other is an ordinary free-text field, reviewed and exported as information.
- Roles are assigned while the variant is a draft and stay with it for every scan.
- A barcode can hold a role only with the CODE128 encoding, the only one that can encode the role tag.
- The variant identifier must be shown on all pages ("Requires 'Show on all pages' enabled"), so that every sheet carries it. Since "Show on all pages" copies an object only onto the pages added afterwards (2.6), the variant code must be placed and shown on all pages **before** the other pages are added: pages that already exist do not receive it, and publishing does not check it (12).
- A role held by an object shown on all pages applies to **all its linked copies**: the copies of a code with a role are used for identification only and produce no reading on any page; the copies of a respondent-ID grid are read as respondent-ID grids; the copies of a handwritten identifier are reviewed as identifiers. The copies cannot be unlinked while one of them holds a role.
- An object holding a role cannot be deleted: deletion is refused until the role is removed ("Remove role").
- Printed codes with a role carry a tag that says what they are, so that a sheet can be identified before its variant is known; codes without a role are printed as they are. A respondent code prepared outside FormScanner (for example a sticker) must use the same tagged format to be recognised.

**Respondent-ID grid.** The label of each row of a `MATRIX` with the `RESPONDENT_IDENTIFIER` role is the character the identifier takes for that column. On such a grid, row labels cannot be empty (or blank) and cannot contain `?`, which marks a column with no mark (7.3). Assigning the role to a grid with such a label, creating or renaming a row with such a label on a grid with the role (also on a linked copy), and relinking copies that would bring the role onto such a label are all refused with a message. Without the role, labels are free.

### 4.2 Variant identifier and page number

The value of the variant identifier code tells the variants apart: two published variants of a project cannot have the same value, and publishing a variant whose value is already published is refused.

On the variant identifier code, the option **"Include page number"** prints, on each page, the variant code together with that page's number. It is available only on a code with the variant identifier role (or one of its linked copies), it is off by default, and it is switched off when the role is removed. With it, every sheet declares its own position in the submission, whatever the order of the files (5.7).

### 4.3 Reserved bands

Four **reserved bands**, 15 mm wide, run along the four edges of every page. They hold the alignment marks (6.1) and no object may lie in them, not even a hidden one:

- the editor shows the bands on every page and does not let an object be placed, moved or resized into them; an object already in a band is highlighted ("Inside the reserved band: move it inside the page area.");
- the server applies the same rule when an object is saved (`OBJECT_IN_RESERVED_BAND`);
- a draft variant with objects in the bands cannot be published: the refusal lists the objects and their pages (`OBJECTS_IN_RESERVED_BANDS`);
- objects are never moved automatically: the layout stays the author's choice.

### 4.4 Maximum score

The maximum score of the variant is shown in the variant bar ("Max score"), also when it is zero and also on a published variant, and is updated at every save that changes it. It is computed with the same formula used for the results (9).

## 5. Ingestion and matching

### 5.1 Batches

A **batch** (a scan session) collects the scanned sheets of one form project. It always belongs to exactly one project and may contain submissions of different variants of that project (for example versions A, B and C of the same exam).

A batch is created, and sheets are uploaded to it, only while the project is `ACTIVE` and ready for scanning (5.2). Both checks are repeated at every upload, so a batch created earlier stops accepting sheets if the project is closed or stops being ready. Every other operation on sheets already uploaded (matching, extraction, manual variant assignment, threshold changes, review, export) remains available.

### 5.2 Scan readiness

Before any sheet is accepted, the project's published variants must be distinguishable:

| Published variants | Ready for scanning? |
| --- | --- |
| None | No: "No published variant exists for this project." |
| Exactly one | Yes. Every submission is assigned to that variant; no code needs to be read. |
| More than one, at least one with a `VARIANT_IDENTIFIER` | Yes. Each submission is matched by reading its variant code (5.6). |
| More than one, none with a `VARIANT_IDENTIFIER` | No: "Multiple published variants exist but none has a VARIANT_IDENTIFIER role assigned; scanning cannot automatically match sheets to a variant." |
| Two published variants with the same variant identifier value | No: "Multiple published variants share the same VARIANT_IDENTIFIER value; scanning cannot reliably match sheets to a variant." |

When the project is not ready, the project page shows the reason, and both batch creation and upload are refused with the same message (`NOT_SCAN_READY`). There is no way to bypass the check.

**Rationale:** a configuration problem of the project is caught before any sheet is uploaded, instead of being discovered halfway through a batch as hundreds of submissions that cannot be matched.

### 5.3 Input formats

Sheets can be uploaded as:

- **PDF files**, up to 50 MB each. **One PDF is one submission**, whatever its number of pages; each page is rendered as an image at 150 dpi.
- **Loose images**, one sheet per image, in JPEG or PNG format, up to 20 MB each. The format is recognised from the file content, not from its name or extension.

From the interface, files are selected by their extension (`.pdf`, `.jpg`, `.jpeg`, `.png`); other files are ignored with a message. A file that is empty, too large or of an unsupported type is rejected with the reason; the other files of the same upload are accepted.

The recommended input is **one PDF per submission**: the file itself says which sheets belong together, and the case of a sheet that cannot be grouped (`UNASSIGNED`, 5.9) never arises. The upload area says so. Loose images are fully supported, but they depend on codes or file names to be grouped.

### 5.4 Codes read at ingestion

When a sheet is uploaded, every QR code and barcode on it is decoded. Codes printed with a role carry a tag that says what they are (4.1):

- the **variant code** (`VARIANT_IDENTIFIER`) names the variant and, if printed with the page number (4.2), the position of the sheet in the submission;
- the **respondent code** (`RESPONDENT_IDENTIFIER` on a CODE object) names the candidate. Read on any sheet of a submission, it becomes the submission's candidate, unless the submission already has one;
- any other code is ignored at this stage, so a third-party code can never be mistaken for an identifier.

A code that cannot be decoded is a normal outcome, not an error: the sheet is placed by the fallback rules below.

### 5.5 Grouping sheets into submissions

A **submission** is the set of sheets filled in by one respondent on one variant.

- **PDF:** the file is the submission.
- **Loose images:** each image is assigned to a submission by, in order:
  1. a readable respondent code: images with the same respondent code form one submission;
  2. otherwise, a file name that follows the naming convention (5.5.1): images with the same group token form one submission;
  3. otherwise the image cannot be grouped: it becomes a single-sheet submission in the `UNASSIGNED` state (5.9).

Grouping applies to the whole batch, across uploads: `smith_page_1.jpg` uploaded today and `smith_page_2.jpg` uploaded tomorrow belong to the same submission.

When an image has both a respondent code and a conforming file name with a different group token, the **respondent code wins** (it is printed on the sheet, the file name is typed by a person). The upload is not blocked and the mismatch is recorded in the audit trail.

The variant code never decides the submission. It is identical on the same page of every respondent's sheet, so it can say which page a sheet is, but not whose.

#### 5.5.1 File naming convention

```
<group>_page_<n>.<ext>
```

- `<group>` is the grouping token: every image with the same token belongs to the same submission. It must not be empty and keeps its case (`A` and `a` are two different groups).
- `_page_` is the separator. `_pagina_` is accepted as an equivalent. The separator is case-insensitive, and the **last** separator in the name is the one that counts, so the group token may itself contain `_page_`.
- `<n>` is the page number, starting at 1. Leading zeros are accepted (`_page_01`); signs, spaces and decimals are not.
- The extension plays no part in grouping, and the file type is checked on the content (from the interface the file must still have one of the extensions in 5.3).

Examples: `smith-john_page_1.jpg` (group `smith-john`, page 1); `ROOM3_DESK12_page_02.png` (group `ROOM3_DESK12`, page 2).

### 5.6 Variant matching

Matching runs automatically as part of every upload. It decides the variant of each submission:

1. If the project has exactly one published variant, every submission is assigned to it.
2. Otherwise the sheets of the submission are examined in order until one carries a variant code whose value belongs to a published variant of the project. One readable sheet is enough for the whole submission.
3. If no sheet answers, the submission goes to `MATCH_FAILED` (5.8), and the reason is recorded:
   - no readable variant code on any sheet;
   - a variant code was read, but it is not the code of a published variant (the value read is shown);
   - the project has no published variant.

Matching never changes a decision already taken: a later upload that adds a readable sheet to a `MATCH_FAILED` submission can resolve it, but nothing already resolved is recomputed. Matching never touches a submission marked unresolvable (5.10), and it never changes the state of an `UNASSIGNED` submission, even when it resolves its variant: the open question there is whose sheet it is, which only a person can answer.

### 5.7 Page position and missing pages

Once the variant is known, each sheet is bound to a page of the variant:

1. if its variant code carries a page number, that page;
2. otherwise, for a loose image with a conforming file name, the page number in the name;
3. otherwise, its position in the sequence: the page order in the PDF, or for loose images the first free position in the group, in order of arrival.

A sheet whose page does not exist in the variant (more sheets than pages, or a code claiming a page out of range) stays in the submission without a page and is not read. One sheet that cannot be placed never prevents the others from being placed.

Two sheets can claim the same page, for example a page scanned twice, or a sheet of another submission whose variant code claims a page already present. Both are kept and bound to that page (**duplicate sheets**). Wherever one reading per object is needed (candidate, score, export), the reading on the first sheet in physical page order counts.

**Rationale:** when sheets are not in the expected order, a PDF from an automatic document feeder can be out of order too: the page number in the variant code makes every sheet place itself, whatever the order of the file. Without it, the order of the file is assumed to be correct. Keeping both duplicates is the only choice that loses no scanned sheet.

A submission may lack some pages of its variant (a page lost, not scanned or unreadable). This never blocks processing: the submission moves through its states on the basis of the pages present, remains fully reviewable, and its **missing pages** are listed in the queue, in the submission view and in the export. Objects on a missing page have no reading: for scoring they are worth zero, and the maximum score does not change (9).

### 5.8 Submissions whose variant is unknown (MATCH_FAILED)

A `MATCH_FAILED` submission is shown in the queue with the reason of the failure and the filter "To assign". It is a recoverable state: the reviewer assigns the variant by hand, choosing among the published variants of the project. The action is "Assign variant", on one submission or on several selected together. The submission moves to `MATCHED`, its sheets are bound to the variant's pages, and it is read at the next "Run extraction" (7.1); the assignment dialog reminds the reviewer of this. Manual assignment is available only on a `MATCH_FAILED` submission and only towards a published variant.

If the sheets are too damaged to tell the variant, the reviewer marks the submission unresolvable (5.10).

### 5.9 Sheets that cannot be grouped (UNASSIGNED)

An `UNASSIGNED` submission is a single loose image that could be grouped neither by respondent code nor by file name. Nothing can be done to place it in another submission: the only action is to mark it unresolvable (5.10). Until then it blocks the export of the batch (10).

**Rationale:** a sheet must never disappear from the results without a human decision.

### 5.10 Unresolvable submissions

A reviewer can mark a `MATCH_FAILED` or `UNASSIGNED` submission as **unresolvable**, one submission at a time and after a confirmation. It moves to `UNRESOLVABLE`: it is closed, it is never read or reviewed, it does not block the export of the batch, and it appears in the export with a row without a score (10).

The action is "Mark unresolvable" in the queue. It can be undone with "Undo unresolvable", from the "Unresolvable" filter: the submission returns to the state it was marked from. Matching never touches an unresolvable submission, even when re-run: the human decision stands until a human undoes it.

A batch whose submissions are all unresolvable cannot be exported: at least one submission closed by a review is needed.

## 6. Sheet alignment

A printed and scanned sheet never matches its template exactly: the printer shifts and scales the content slightly, the scanner captures an area that is not exactly A4, and the feeder may pull the sheet in slightly crooked. An offset of two or three millimetres is enough to miss a bubble. Before reading any object, FormScanner therefore aligns every scanned page to its template using marks printed on the sheet.

### 6.1 Alignment marks

Every page of the exported "student" and "solution" PDFs prints, always and for every project, with no setting:

- **four corner marks**, one per corner, 8–13 mm from the edge of the sheet. The bottom-right mark has a different shape from the other three, so the reading can tell the orientation of the page;
- **a column of side marks** along the left and right edges, equally spaced, in the same column as the corner marks and of a different shape, so they are never mistaken for a corner mark.

All marks lie inside the reserved bands (4.3), within the printable area of common printers when printed at 100% scale. The page content is never moved or shrunk to make room for them, and the editor's preview shows the marks exactly as the PDF.

### 6.2 Aligning a page

Before its objects are read, each scanned page (from a PDF or a loose image) is aligned to its template on the corner marks, correcting:

- the **shift**;
- the **scale**, which may differ horizontally and vertically;
- a **small rotation**, such as that of a sheet fed in crooked, up to **±10°**.

Three corner marks out of four are enough, so a mark covered by a hand or a fold does not cost the page. A page scanned **upside down** (for example a stack inserted the wrong way into the feeder) is recognised by the distinct bottom-right mark, turned upright and read normally.

The page is then straightened **in horizontal strips**, each corrected on the side marks that bound it. This corrects the uneven stretching an automatic document feeder (ADF) produces along the feed direction (about 1 mm at mid-page, none at the corners), which the corner marks alone cannot see. A missing side mark (a fold, a pen stroke on the margin) is skipped and its strip is corrected from its neighbours. If the side marks are not enough or not consistent, or the sheet was printed without them, the page is aligned on the corner marks alone, with no new review reason: the side marks can only improve a reading, never make it worse.

With this correction, both the flatbed and the automatic document feeder are reference inputs. In the feeder, sheets must be loaded in portrait orientation (short edge first).

### 6.3 Pages that cannot be aligned

If the alignment cannot be computed (marks covered or outside the scanned area, a sheet printed from a PDF without marks) or the result is not plausible (for example a rotation beyond the limit), the page is read in proportion to the whole image, as if there were no marks, and **every object on the page goes to review** with the reason `SHEET_NOT_ALIGNED` ("Sheet not aligned"), whatever its confidence. In the submission view the page group shows the warning "Sheet N not aligned: readings may be shifted. Check each one against the full sheet." These objects are excluded from "Accept all remaining" and must be confirmed one by one (8).

**Rationale:** on a page that is not aligned, readings may be shifted systematically. An uncertain reading is neither discarded nor accepted silently: the reviewer confirms or corrects it looking at the sheet.

### 6.4 Alignment and the rest of the pipeline

- The **variant code** does not depend on alignment: it is read at upload, anywhere on the whole image.
- Alignment is a step of reading, not an option. The straightened image is the one the extraction reads and the one shown in review, both in the object crop and in "Show full sheet" (8): what the reviewer sees is exactly what was read. A page that could not be aligned is shown as it was read.
- Re-reading after a threshold change (7.5) or a later "Run extraction" applies the same rules.

## 7. Extraction

Extraction reads the answers off the aligned sheets and decides, for each reading, whether a human must look at it.

### 7.1 Running extraction

Extraction is started by hand, for the whole batch, with "Run extraction" in the batch page; the outcome of the last run is shown next to it. Uploads match sheets automatically but never extract them.

**Rationale:** matching compares a few stored strings, while extraction opens every page image; on a batch of hundreds of submissions it takes minutes. That is work the operator asks for when ready, not something an upload does silently.

- A run reads the `MATCHED` and `SCANNED` submissions of the batch. It never reads a closed submission (`REVIEWED`, `FINALIZED`), nor a `MATCH_FAILED`, `UNASSIGNED` or `UNRESOLVABLE` one, nor a sheet that is not bound to a page of the variant (5.7).
- A run never reads an object twice: an object that already has a reading is skipped, and existing readings, including the reviewed ones, are never overwritten. Pressing "Run extraction" again is always safe, and reads only what is still unread.
- One failure never stops the run: an unreadable page image produces a counted error and the run continues with the next object.
- At the end of a submission, if no object failed, a `MATCHED` submission moves to `SCANNED` ("extracted, ready for review"); this happens even if some of its pages are missing. A submission with errors stays `MATCHED` and the next run retries only the objects that failed.
- **One run at a time per batch**, even from different users. While a run is in progress, a second run, a change of the confidence threshold, "Review anyway", and closing or reopening a submission of the batch are refused at once with a warning (`EXTRACTION_RUNNING`), and can be repeated when the run is over.

### 7.2 Confidence and validity

Each reading of a question is judged on two independent axes:

- **Confidence**: how sure the system is of the optical reading of the marks, from 0 to 1. A bubble counts as marked when it is filled in at least by half; the fraction filled in is the confidence of that mark.
- **Validity**: whether the combination of marks read respects the rule of the question set in authoring: `VALID`, `NO_ANSWER` (nothing marked) or `TOO_MANY_ANSWERS` (more than one mark where only one is allowed).

The two axes are never merged into one number: a reading can be fully confident and not valid at the same time (the system is certain the respondent marked two bubbles on a single-choice question). Scoring uses both (9).

### 7.3 Reading each object type

**QUESTION.** The reading lists the selected options, each with its confidence; no confidence is recorded for unselected options. Validity is `NO_ANSWER` if nothing is selected, `TOO_MANY_ANSWERS` if more than one option is selected on a single-choice question, `VALID` otherwise. The confidence of the reading is the lowest among the selected options.

**MATRIX (without a role).** Each group of the matrix behaves as an independent mini-question, with the same confidence and validity rules. The selection rule is one for the whole matrix and is chosen in authoring: one or several selections per row, one or several per column, or free. With a per-row or free rule each row is a group; with a per-column rule each column is a group. No mark in a group gives `NO_ANSWER`; more than one mark with a single-selection rule gives `TOO_MANY_ANSWERS`. The matrix needs review if any of its groups does. Only a matrix marked "Evaluative" in authoring is scored (9).

**Respondent-ID grid (MATRIX with the `RESPONDENT_IDENTIFIER` role).** Not a question: it has no validity and is never scored. Each column is one character of the identifier, and the character is the label of the marked row. The reading is the raw value (`?` for a column with no mark) and an aggregated confidence: the **average** over all columns, with 0 for an empty column. A column with more than one mark (ambiguous digit) is written with the most filled-in mark and flagged. A grid without a readable value goes to review whatever its confidence (7.4).

**Rationale:** the respondent ID decides to whom every answer of the submission belongs. A wrong answer costs a point; a wrong respondent ID hands the whole submission to someone else. Hence the average, which a single empty column lowers, and the forced review of every incomplete or ambiguous value.

**FREE_TEXT_AREA.** Handwriting is read by OCR as a suggestion, with a confidence shown for information. The reading **always** goes to review, whatever the threshold. The OCR language is set for the whole installation in the deployment configuration (English by default; several languages can be combined); a language that is configured but not installed prevents the backend from starting, with a message naming it.

**Rationale:** OCR of handwriting is never reliable enough to be accepted unseen. This is a design rule, not a consequence of a low confidence.

**CODE without a role.** The QR code or barcode found in the object's area is decoded and its value is kept as information; it is never scored. There is no graded confidence: decoding succeeds or fails. A code carrying a role tag (a variant or respondent code) is never taken as the value of a CODE without a role.

**Objects that are not read.** TEXT and IMAGE objects, and CODE objects with a role (their codes were already used at upload, 5.4), produce no reading, including on their linked copies.

### 7.4 Review required and review reasons

A reading **needs review** when:

| Object | Needs review when |
| --- | --- |
| QUESTION, MATRIX | the lowest confidence among the selected marks is below the threshold, **or** the validity is not `VALID` (in at least one group for a MATRIX) |
| Respondent-ID grid | the aggregated confidence is below the threshold, **or**, whatever the confidence, a column is ambiguous, a column is empty, or the grid has no rows or columns |
| FREE_TEXT_AREA | always |
| CODE without a role | decoding failed |

For every object type, a reading taken on a page that could not be aligned also needs review (6.3).

Each reading in review states why, with one or more reasons:

| Reason | UI label | Meaning |
| --- | --- | --- |
| `SHEET_NOT_ALIGNED` | "Sheet not aligned" | the page could not be aligned; the reading may be shifted |
| `LOW_CONFIDENCE` | "Low confidence" | the confidence is below the batch threshold |
| `INVALID_ANSWER` | "Invalid answer" | the marks read break the rule of the question (`NO_ANSWER`, `TOO_MANY_ANSWERS`) |
| `ALWAYS_REVIEW` | "Always reviewed" | handwritten text, always checked by a person |
| `DECODE_FAILED` | "Code not decoded" | the code did not decode |
| `AMBIGUOUS_DIGITS` | "Ambiguous digits" | a column of the respondent-ID grid has more than one mark |
| `MISSING_DIGITS` | "Missing digits" | a column of the respondent-ID grid has no mark |
| `UNREADABLE_GRID` | "Grid not readable" | the respondent-ID grid has no rows or columns: nothing could be read |
| `EXTRACTION_FAILED` | "Could not be read" | no run could read the object; placeholder of "Review anyway" (7.7) |

A reading on a page that could not be aligned lists `SHEET_NOT_ALIGNED` first, together with any other reason it has.

A reading that did not need review is accepted as read: it does not appear among the objects to review and it counts as it was read.

The number of readings still to review is computed on the fly for every submission, never stored, so it always reflects the current threshold and the current reviews.

### 7.5 Confidence threshold

The threshold below which a reading goes to review has a system-wide default of **0.7**. Each batch can override it at any time ("Confidence threshold", "Edit", as a percentage) and return to the default ("Reset to system default"); the batch page shows the threshold in force and whether it is "custom" or the "system default". An override affects only its own batch.

Changing the threshold of a batch takes effect retroactively, but selectively: the review need is recomputed only for readings **not yet reviewed** of submissions **still open**. Readings already confirmed, replaced or flagged by a reviewer are never changed, and closed submissions are not touched (reopening a submission applies the threshold in force, 8). The change is refused while an extraction run is in progress (7.1).

**Rationale:** the operator adapts the sensitivity to what they see in the review queue, without disturbing work already done by a human.

### 7.6 The candidate from the readings

FormScanner keeps no register of respondents: the **candidate** of a submission is a raw identifier (a student number, a code, a name) read from the sheet or typed by a reviewer. Matching it to a real person happens outside the system.

The candidate is set automatically as follows:

- a **respondent code** read at upload becomes the candidate (5.4);
- a **respondent-ID grid** read without needing review (above the threshold, complete, not ambiguous) becomes the candidate without confirmation;
- a reading that needs review sets the candidate only when a reviewer confirms or replaces it (8). This includes every handwritten respondent identifier (`FREE_TEXT_AREA` with the role), which is always reviewed.

Limits of the automatic candidate:

- it never replaces a candidate already present (typed by hand or confirmed in review);
- it never fills the candidate of a submission on which anything has already been saved in review: a candidate deliberately emptied by a reviewer stays empty;
- when a submission has several readings of the identifier (linked copies of the object on several pages, duplicate sheets), only the **first in physical page order** counts. If that one needs review, no candidate is set automatically, even if a later sheet reads cleanly.

A threshold change follows the same rule: a respondent-ID grid that leaves review because the threshold was lowered becomes the candidate of an open submission that has none. A grid that goes back to review because the threshold was raised keeps the candidate it gave: the candidate is **never cleared automatically**.

When the variant has a respondent identifier but the submission has no candidate (reading failed, field left empty), the queue shows "No candidate"; the reviewer can type the candidate in the submission header. A submission can be closed without a candidate, so that anonymous questionnaires are not penalised.

### 7.7 Review anyway

A `MATCHED` submission stays `MATCHED` as long as an object of its sheets fails extraction, and a torn corner or a bad scan can make that permanent. "Review anyway" brings it into review: it is offered only after a run has examined the submission. Every object that no run could read gets a placeholder reading, which is in review with the reason `EXTRACTION_FAILED`, whatever the threshold; the reviewer enters the answer from the paper sheet or flags it. A later run does not retry these objects. The submission moves to `SCANNED`.

"Review anyway" is refused while an extraction run is in progress on the batch (7.1).

## 8. Review

Review is where people confirm or correct what extraction was not sure about. It works on three levels, from top to bottom: the **batch queue**, the **submission** and the **single reading**.

### 8.1 The batch queue

The queue is the batch page itself. Next to the list of submissions it holds "Run extraction" with the outcome of the last run (7.1), the confidence threshold (7.5), the export commands (10) and, once the batch has submissions, "+ Add files" to upload more.

For each submission the queue shows: its short id, the candidate (or "⚠ No candidate" when the variant has a respondent identifier and none was read), its state, its variant, its pages ("⚠ Missing p. …" for missing pages), the number of readings still to review and who is reviewing it (a lock indicator such as "🔒 You · 3 min ago"; expired locks are not shown). A `MATCH_FAILED` submission shows the reason of the failure (5.6), a `MATCHED` one the number of objects still unread. On a closed submission the queue shows how many readings were left unresolved ("N unresolved"), separately from those still to review.

Filters, each with its count: "All", "To assign" (`MATCH_FAILED`), "To extract" (`UPLOADED`, `MATCHED`), "To review" (readings still to review, submission open), "Ready to close" (`SCANNED` with nothing left to review), "Reviewed" (`REVIEWED`, `FINALIZED`), "Unresolvable", "Unassigned". Submissions are listed in upload order.

The actions available from the queue are "Assign variant" (5.8), "Mark unresolvable" and "Undo unresolvable" (5.10) and "Review anyway" (7.7). A `SCANNED` or closed submission opens in the submission view.

The queue is refreshed after every action and with "↻ Refresh"; it is never polled.

All members see all the submissions: there are no personal queues. Several reviewers can work on the same batch at once, for example teachers sharing the marking.

### 8.2 The submission view

One page shows a submission, with the list of readings and the focus on the selected reading side by side; there is no separate page per reading.

**Header:** the variant, the candidate, the pages (with the missing ones), the readings still to review ("To review: N of M remaining"; on a closed submission "Unresolved: N of M at closing") and the score (9.7). The candidate can be edited by hand ("Edit", while holding the lock); emptying the field clears it.

**List of readings:** only the readings that needed review, grouped by sheet ("Page P · sheet N"; "Unplaced sheet N" for a sheet not bound to a page) and, within a sheet, in reading order. Readings accepted automatically do not appear ("Nothing to review: every reading was accepted automatically."). Readings already resolved stay in the list, shown muted with their outcome (✓ confirmed, ✎ replaced, ⚑ flagged), so the reviewer can go back to them. A sheet that could not be aligned carries its warning (6.3). The reviewer can select any reading, in any order.

**Focus on a reading:**

- the crop of the scanned sheet around the object, and "Show full sheet" to see the whole page. The image is the aligned one the extraction read (6.4); it is shown at a resolution sufficient to read bubbles and handwriting;
- the proposed reading, in the form suited to its type: the selected options with their confidence ("A (96%), C (40%)"), for a matrix one line per group, the identifier value with its ambiguous columns, the OCR text, the decoded code;
- the review reasons, each with an explanation ("Why it is here", 7.4);
- for a reading of the respondent identifier that does not set the candidate, which reading does (8.6);
- the actions, and the outcome already given, with who gave it and when.

### 8.3 Review actions

Four actions are available on every reading in review:

| Action | Button | Effect |
| --- | --- | --- |
| Confirm | "Confirm" | The reading is right as read. |
| Replace | "Replace…" | The reading is wrong; the reviewer enters the right value. |
| Flag | "Flag unresolvable" | The answer cannot be determined from the sheet (sheet damaged, answer irrecoverably ambiguous): no answer, with a reason of its own. |
| Undo | "Undo" | Takes back the outcome of a resolved reading, which becomes unresolved again. |

- **Confirm** is not offered when there is nothing complete to confirm: a placeholder of "Review anyway", a code not decoded, an empty or incomplete respondent identifier, or a handwritten identifier longer than 500 characters. The reviewer replaces or flags it.
- **Replace** opens an editor suited to the type: the options of a question (letters A–Z toggle them), the grid of a matrix, a text field for an identifier, a handwritten text or a code. The validity of the value entered is always computed by the system, with the extraction's rules: replacing with no option selected means "the respondent did not answer" (`NO_ANSWER`), which is different from a flag.
- **Undo** is available only on a resolved reading: to postpone an unresolved one, the reviewer simply moves on.
- Confirm, replace and flag can also be applied to a reading already resolved, replacing its outcome.
- Every action may carry an optional **note** ("+ Add note"), visible to the other reviewers who open the submission; the note shown is the last one.
- Every action is recorded, with who did it and when, in a history that is never modified (undoing is itself an entry).
- After confirm, replace or flag, the selection moves to the next unresolved reading; after undo it stays.

The three ways a reading can end up without an answer remain distinct in review: no answer from the respondent (validity `NO_ANSWER`), flagged by a reviewer, unresolved. They all score 0 (9.4).

### 8.4 Keyboard

The review is designed to be done from the keyboard:

| Key | Effect |
| --- | --- |
| Enter | Confirm the proposed reading and move to the next one to review |
| R | Replace |
| F | Flag |
| U | Undo |
| ↓ or J / ↑ or K | Next / previous reading |
| Esc | Close the editor or the note without saving |

In the replace editor, A–Z toggle the options of a question, Enter (or Ctrl/⌘+Enter) saves and Esc cancels. Shortcuts are inactive while typing in a field or while a dialog is open.

### 8.5 Accept all remaining

"Accept all remaining (N)" confirms in one operation, after a confirmation showing their number, all the readings of the submission still to review that can safely be accepted unseen. Excluded, and left to be reviewed one by one:

- handwritten text (`FREE_TEXT_AREA`), including a handwritten identifier: an unchecked OCR reading of a name could attribute the submission to the wrong person;
- respondent-ID grids with an ambiguous or missing digit, or unreadable;
- codes that could not be decoded, and placeholders of "Review anyway";
- every other reading that cannot be confirmed (8.3);
- readings on a sheet that could not be aligned: they may be shifted systematically and must be checked against the sheet (6.3).

What remains are readings sent to review by a low confidence or an invalid combination: exactly what a reviewer who has looked at the sheet and found nothing wrong wants to accept at once. "Accept all remaining" is a single save for the purposes of the lock and the version (8.7).

**Rationale:** there is deliberately no bulk action on a whole batch. It would override the other reviewers' locks, be hard to undo, and amount to lowering the confidence threshold, which already exists per batch (7.5).

### 8.6 The candidate in review

- Confirming or replacing a reading of the respondent identifier sets the candidate of the submission to the confirmed or entered value. Flagging or undoing never changes the candidate.
- When the submission holds several readings of the identifier (linked copies, duplicate sheets), only the **first in physical page order** sets the candidate, also in review and also through "Accept all remaining". Outcomes on the other readings are recorded but leave the candidate alone, and the focus says so ("Does not set the candidate: only the first reading in page order does."). If the first reading is flagged, no later one takes its place: the candidate is typed in the header.
- The candidate typed in the header is a review save like any other.

### 8.7 Several reviewers on one submission

Two mechanisms, both per submission, keep reviewers from overwriting each other.

**The lock** says who is working on a submission.

- Opening a `SCANNED` submission takes the lock if it is free. The lock is renewed when the submission is opened and at every save, **expires after 15 minutes of inactivity**, and is released on closing the submission or leaving it (if the browser is closed, the lock simply expires).
- Opening a submission locked by someone else shows who and since when ("🔒 In review by Anna, 3 min ago. You can look, but not make changes.") and offers "Take over". Nobody is ever blocked, and no administrative unlock exists: anyone can take over.
- The lock is binding on saves. Whoever lost it to a take-over is refused at the next save; the view becomes read-only ("Anna took over this submission 1 min ago. Your view is now read-only.") and offers "Take back". When the lock is free or expired, the first save takes it.

**The version** guarantees that nobody saves from an outdated view. Every save increments the version of the submission and declares the version it was made on; if they differ, the save is refused and the view reloads the current state of the whole submission ("This submission was changed by someone else. It has been reloaded.").

**Rationale:** the lock says *who* is working; the version guarantees that *nobody* saves on a stale state. Example: Anna leaves a submission open for more than 15 minutes; Ben takes it, reviews and saves; when Anna comes back and saves, she is refused, warned and shown the current state, and Ben's work is intact. The version is checked per submission, not per reading, to cover this case even when Anna saves a reading Ben never touched.

Closing and reopening a submission are refused while an extraction run is in progress on the batch (7.1); review actions are not, since a run never reads a submission's objects twice.

### 8.8 Closing a submission

Closing (`SCANNED` → `REVIEWED`) is always an explicit action ("Close submission"), even when nothing is left to review. It requires holding the lock.

If, at that moment, there are unresolved readings, the candidate is missing (when the variant has a respondent identifier) or pages are missing, a confirmation lists them ("3 objects are still unresolved. They will count as no answer (0 points) and as not reviewed in the export."; "No candidate."; "Missing page: 2."). None of them blocks the close; without any of them the submission closes at once.

On closing:

- the submission becomes read-only and its lock is released;
- its unresolved readings stay unresolved: they score 0 and are counted as "Unreviewed objects" in the export;
- the score computed at that moment is saved (9.7);
- the reviewer stays on the submission.

A threshold change no longer touches a closed submission (7.5).

### 8.9 Reopening a submission

Any reviewer can reopen a `REVIEWED` submission ("Reopen submission"), without confirmation:

- it returns to `SCANNED` with the lock assigned to whoever reopened it;
- every outcome already given is kept;
- its unresolved readings follow the confidence threshold now in force in the batch, as in a submission that stayed open;
- the saved score is cleared, and saved again at the next close;
- the reopening is recorded in the audit trail (who, when).

Only a `REVIEWED` submission can be reopened.

### 8.10 Moving on

"Next submission →" opens the first submission after the current one in queue order, wrapping round to the top, that is `SCANNED`, not locked by someone else, and within the filter the reviewer came from: "To review" goes to submissions with readings to review, "Ready to close" to those with none left, "All" to both.

## 9. Scoring

### 9.1 Principles

The score reuses what the author configured on the options and cells, with no further setting:

- every option of a `QUESTION`, and every cell of an evaluative `MATRIX`, may have a score, positive, zero or negative;
- an option with no score configured is worth **0**, even if it is marked correct: the "correct" flag is used by the "solution" PDF, not by scoring.

One principle governs every rule below: **an answer that is not univocal never earns positive points.**

### 9.2 Scoring a question

The reading scored is the current one: the value entered by the reviewer if the reading was replaced, otherwise the extracted one. Then, with `raw_score` the sum of the scores of the selected options:

| Case | Score |
| --- | --- |
| **Full answer**: every option selected, on a question with at least two options | 0, always |
| `NO_ANSWER` | 0 |
| `VALID` | `raw_score` |
| `TOO_MANY_ANSWERS` (single-choice only) | `min(0, raw_score)`: never positive |

The full answer is checked first: a single-choice question with every option marked scores 0 even where `min(0, raw_score)` would be negative. It is a rule of scoring, not a validity: extraction and review keep such a reading `TOO_MANY_ANSWERS` (single-choice) or `VALID` (multiple-choice). With a single option there is nothing to choose between, and selecting it is an answer like any other.

**Rationale:** marking every option is not a set of mistakes; it is the same as making no choice, a disguised no-answer. The same formula serves single-choice and multiple-choice questions alike, with no special rule per type.

### 9.3 Scoring a matrix

Only a `MATRIX` marked "Evaluative" and without the `RESPONDENT_IDENTIFIER` role is scored; any other matrix has no score, does not count for the total or the maximum, and has no points columns in the export.

Each group of an evaluative matrix (the row, or the column under a per-column selection rule, as for validity) is scored as an independent question with the table above. The options of a group are **all its printed bubbles**: a cell without a score is worth 0, and a full answer means every bubble of the group marked (if there are at least two). The score of the matrix is the algebraic sum of its groups' scores, with no all-or-nothing rule.

### 9.4 No answer, flagged, unresolved

Three different situations leave an object without a determined answer, and all three are treated alike: **0 points, counted in the total**.

| Case | Meaning |
| --- | --- |
| No answer | The respondent did not answer: validity `NO_ANSWER`, or a reading replaced with nothing selected. |
| Flagged | The reviewer declared the answer impossible to determine (8). |
| Unresolved | The reading needs review and no reviewer has resolved it (left without an outcome, or reviewed and then undone). |

- An unresolved reading is worth 0 **while the submission is open too**: the score shown during review is what closing the submission at that moment would save.
- A reading that never needed review is not unresolved: it is worth what was read.
- A flag is an outcome of the whole object: on an evaluative matrix it zeroes every group. To keep the readable groups, the reviewer replaces the reading and leaves the unreadable groups empty.
- The three cases remain distinguishable in review; the export shows how many objects were left unresolved per submission (10).

**Rationale:** the reviewer always has the paper sheet at hand. A flag therefore means the answer is unreadable on paper too (for example deliberately made illegible), not a fault of the scanning process: it is the same as no answer, and nothing needs to be excluded from the total.

### 9.5 Which reading counts

When a submission holds several readings of the same object, the **first in physical page order** counts, for the score and for the export:

- **duplicate sheets** (5.7);
- **linked copies** (2.6): an object shown on all pages is one question, counted once; a copy detached from the others is an object of its own.

An object whose page is missing from the submission has no reading: it is worth 0 and the maximum does not change.

### 9.6 Maximum score

The maximum score of a variant is the sum of the maxima of its scored objects (questions and evaluative matrices), and is the maximum actually reachable with the rules above, so that 100% is always attainable:

- single-choice question, and group of a matrix that allows one selection: the highest option score, never below 0;
- multiple-choice question, and group of a matrix with several selections or a free rule: the sum of the positive option scores; if every option is positive and there are at least two, the lowest is subtracted, because marking them all is a full answer worth 0;
- evaluative matrix: the sum of the maxima of its groups.

Linked copies count once. The maximum is fixed for a variant: since no-answer cases are never excluded, every submission of the same variant has the same maximum, and no normalisation is needed. The same computation is shown in authoring (4.4) and in the results.

### 9.7 Score of a submission

The score of a submission is always shown as three values: **points obtained**, **maximum points** and **percentage**.

Example: "12 / 18 (66.7%)". Points are shown with up to two decimals, the percentage with one. The score is shown in the header of the submission in review and in the Summary export; the maximum of the variant is also shown in authoring (4.4).

- With a maximum of 0 the percentage does not exist and is shown as "—".
- A negative score, possible with negative option scores, stays negative, in the percentage too.
- A submission has a score only once it has been extracted: in review (`SCANNED`) or closed (`REVIEWED`, `FINALIZED`). In any other state it has none, even when its variant is already known: before extraction, "0 / N (0%)" would look like a real grade.

During review the score is computed on the fly and updated after every action. When the submission is closed, the score computed at that moment is saved as its official score; reopening clears it, and the next close saves it again. No history of past scores is kept.

## 10. Results export

### 10.1 When a batch can be exported

A batch can be exported only when its work is complete: every submission is closed by a review (`REVIEWED`, `FINALIZED`) or unresolvable (`UNRESOLVABLE`), and at least one is closed by a review. There is no partial export while the batch is being processed.

Until then the export is disabled and says how many submissions are missing, for each reason:

| Reason | States |
| --- | --- |
| to extract | `UPLOADED`, `MATCHED` |
| in review | `SCANNED` |
| to assign | `MATCH_FAILED` |
| unassigned | `UNASSIGNED` |

A `MATCH_FAILED` submission blocks the export until a reviewer assigns its variant or marks it unresolvable; an `UNASSIGNED` one until a reviewer marks it unresolvable (5.8–5.10).

Every member of the project can export, in any status of the project.

The export commands, "Export summary…" and "Export detail…", are in the batch page. When the batch is not ready they are disabled, with the missing counts ("Not ready: 2 to extract · 5 in review · 1 to assign").

In the list of a project's batches, each batch shows how many of its reviewable submissions (all except the unresolvable ones) have been closed, how many readings are still to review, and whether it is "Ready for export", with the number of unresolvable submissions next to it.

### 10.2 Formats and file names

Two exports are available, each with its own command, opening the same dialog:

- the **Summary export**: one row per submission;
- the **Detail export**: the Summary columns, plus columns per question.

In the dialog the user chooses the format and the file name:

- "CSV — comma separated": "," between fields, "." as decimal mark (proposed);
- "CSV — semicolon separated": ";" between fields, "," as decimal mark, for spreadsheet applications that use a decimal comma;
- "Excel workbook (.xlsx)".

The last format chosen is remembered by the browser.

The proposed name is `Summary_<batch name>_<YYYYMMDD-HHMM>Z` or `Detail_<batch name>_<YYYYMMDD-HHMM>Z`, with the time in UTC; the folder is chosen by the browser. CSV files are UTF-8 (with a byte order mark), one table, no legend.

In both formats no text cell is ever read as a formula (protection against formula injection: the candidate may come from handwriting and the batch name is typed by a user).

### 10.3 Summary export

One row per submission, in upload order (as the queue), with English headers in this order:

| Column | Content |
| --- | --- |
| Submission | The short id of the submission, as in the queue: it tells apart two submissions without a candidate. |
| Candidate | The raw identifier; empty if none. |
| Variant | The variant name. |
| Points | The score saved when the submission was closed. |
| Max points | The maximum score of the variant. |
| Percentage | Points over maximum, in percent (one decimal). |
| Status | The state code, for example `REVIEWED`. |
| Missing pages | The page numbers missing; empty if complete. |
| Unreviewed objects | The readings left unresolved at the close; 0 when the grade is fully verified. |
| Batch | The batch name. |
| Scanned at (UTC) | The upload of the submission's first sheet. FormScanner does not know when a sheet was physically scanned. |

A cell with no value is **empty**, never "—" nor 0, so numeric columns stay numeric: the percentage with a maximum of 0, points and percentage of a closed submission without a saved score, the unreviewed objects of an unresolvable submission.

An **unresolvable** submission has its row, with status `UNRESOLVABLE`, candidate and variant if known, pages, batch and date; points, maximum and percentage stay empty, because it has no score, and an empty cell cannot be mistaken for a 0.

### 10.4 Detail export

The Detail export has every column of the Summary export, followed by the columns per object, **aligned by position**.

**Position.** The reading order of the sheet: page, then top to bottom, then left to right, as the list of objects in review. An object shown on all pages is one position, at its first copy; the groups of an evaluative matrix are consecutive positions, in their order.

**Columns.** Numbered by position within each family, in this order:

| Columns | For | Content |
| --- | --- | --- |
| `Dn`, `Dn_pt` | each question and each group of an evaluative matrix | the answer, and the points it scored |
| `Mn` | each group of a non-evaluative matrix | the answer |
| `Tn` | each `FREE_TEXT_AREA` without a role | the text |
| `Cn` | each `CODE` without a role | the value |

Objects with a role produce no columns: the candidate is already in the Candidate column. A free text or a code more in one variant does not shift the numbering of the scored questions.

**Answers** are written as in review: for a question, the letters of the selected options by position (A the first, B the second…), several separated by ", " ("A, C"); for a matrix group, the labels of the marked columns (or rows), separated by ", ". `Dn_pt` is the value of the scoring for that question (9): a full answer or a `TOO_MANY_ANSWERS` shows the marked letters in `Dn` with 0 or `min(0, sum)` points. The sum of a submission's `Dn_pt` always equals its points.

**No answer.** A single symbol, `*`, marks every object without an answer: no answer from the respondent, flagged, unresolved, or on a missing page; a flag on a matrix gives `*` and 0 on all its groups. A text or a code is exported only when verified (confirmed, replaced, or a code decoded without needing review); otherwise, including an OCR suggestion nobody reviewed, it is `*`. An **empty** cell means instead that the column does not exist for that submission: a variant with fewer positions, or an unresolvable submission, whose columns per object are all empty.

**Rationale:** the distinction between no answer, flagged and unresolved serves the reviewer during review, not the reader of the results. What matters for the results, whether a grade is fully verified, is given per submission by "Unreviewed objects".

**Duplicate sheets:** every column shows the first reading in physical page order, the one that counts for the score.

**The "Columns" sheet.** The xlsx file has a second sheet, "Columns", with one row per column of the export and one column per variant of the batch: the page, the question text (or the object's label) and, for a matrix, the label of its row or column; empty if the variant has no such position. It makes visible the limit of the alignment by position (12).

## 11. Submission lifecycle

| State | Meaning | Reached from |
| --- | --- | --- |
| `UPLOADED` | Just created by an upload, before matching. Matching runs in the same operation, so the state is not normally seen. | upload |
| `MATCHED` | Variant known, sheets bound to its pages, waiting for extraction. | `UPLOADED` (matching), `MATCH_FAILED` (manual assignment, or a later upload adding a readable sheet) |
| `MATCH_FAILED` | The variant could not be determined (5.8). | `UPLOADED` (matching) |
| `UNASSIGNED` | A loose image that could not be grouped (5.9). | upload |
| `UNRESOLVABLE` | Declared unresolvable by a reviewer; closed without a review (5.10). | `MATCH_FAILED`, `UNASSIGNED` ("Mark unresolvable") |
| `SCANNED` | Extracted, ready for review; the only state in which a submission is reviewed. | `MATCHED` (extraction without errors, or "Review anyway"), `REVIEWED` (reopening) |
| `REVIEWED` | Closed by a reviewer; the final state. | `SCANNED` (close) |
| `FINALIZED` | Reserved for a future finalisation step; no action leads to it. | — |

Other transitions:

- `UNRESOLVABLE` → `MATCH_FAILED` or `UNASSIGNED`: "Undo unresolvable", back to the state it was marked from.
- An `UNASSIGNED` submission keeps its state even when matching resolves its variant (5.6).

What each state means for the rest of the pipeline:

| State | Read by extraction | Reviewed | Score | Blocks the export |
| --- | --- | --- | --- | --- |
| `UPLOADED`, `MATCHED` | yes | no | no | yes (to extract) |
| `MATCH_FAILED` | no | no | no | yes (to assign) |
| `UNASSIGNED` | no | no | no | yes (unassigned) |
| `UNRESOLVABLE` | no | no | no | no (row without score) |
| `SCANNED` | yes (only what is still unread) | yes | live | yes (in review) |
| `REVIEWED`, `FINALIZED` | no | read-only | saved at the close | no |

## 12. Known limitations

- **Detail export aligned by position.** If the variants of a batch hold different questions in the same position (for example shuffled versions), the same `Dn` column holds answers to different questions for submissions of different variants. The "Columns" sheet of the xlsx shows what each column is in each variant; the csv has no legend.
- **No perspective correction.** Alignment corrects shift, scale per axis and small rotations, not the perspective of a sheet photographed at an angle: the reference inputs are a flatbed scanner and an automatic document feeder.
- **Feeder in landscape not covered.** The side marks correct stretching along the feed direction only with sheets loaded in portrait orientation; loaded in landscape, the stretching would run along the axis without marks.
- **Strong rotation on a flatbed.** On a flatbed exactly as tall as the sheet, a sheet rotated by more than a few degrees may lose its corner marks outside the scanned image, and the page is then not aligned.
- **Dense barcodes.** PDFs are rendered at 150 dpi: a long value in a narrow barcode may not be readable once printed and scanned. A QR code, or a wider barcode, avoids the problem.
- **Lock lost while reading.** A reviewer who reads a submission for more than 15 minutes without saving loses the lock without noticing; the next save takes it back if nobody else has taken it meanwhile (8).
- **Parallel saves.** If two reviewers really save on the same submission at the same time, one of them must reload, even if they touched different objects; with the lock this happens only after a take-over.
- **A submission extended after extraction.** A `SCANNED` submission that receives a further sheet (a loose image uploaded later with the same group) stays `SCANNED`, and the new sheet is read only at the next "Run extraction". With the recommended input, one PDF per submission, this cannot happen.
- **Variant code on existing pages.** "Show on all pages" copies an object only onto pages added afterwards. A variant code shown on all pages after the other pages were created is missing from them, and publishing does not detect it: those sheets can only be matched through another sheet of the submission or by hand (4.1).
- **No score history.** Reopening and closing again overwrites the saved score; previous values are not kept.

## 13. Out of scope and possible extensions

The following are not part of the product. They are listed so that their absence is not mistaken for an oversight; each may become a feature if a concrete need arises.

- **Manual placement of `UNASSIGNED` sheets**: moving a sheet into an existing submission and choosing its position. Today the only action is to mark it unresolvable.
- **"Page order not verified" indicator**: a badge on the sheets whose position comes from the order of the file rather than from a page number printed in the variant code.
- **Perspective correction**, for sheets photographed with a phone.
- **"Sheet not aligned" indicator in the batch queue**; today the warning is shown in the submission view only.
- **Further alignment marks** (along the top and bottom edges) or a local search around each object, should deformations appear that the side marks do not see.
- **Finalisation** of a reviewed submission (`FINALIZED`) and the rules for reopening it.
- **Score history**, as an audit trail of saved scores.
- **Partial export** of a batch still in progress, and further export formats.
- **A register of respondents**, to match raw identifiers to real people.
- **Finer-grained permissions** than project owner and collaborator.
- **OCR language per project or per user**, chosen from the interface; today it is one per installation.
- **Maximum points per object in authoring**, next to the maximum of the variant.

## Appendix: Glossary

| Term | Meaning |
| --- | --- |
| Batch | A scan session of one project, collecting its uploaded sheets (5.1). |
| Candidate | The raw identifier of the respondent of a submission, read from the sheet or typed by a reviewer; never matched against a register (7.6). |
| Confidence | How sure the system is of an optical reading, from 0 to 1 (7.2). |
| Confidence threshold | The confidence below which a reading needs review; 0.7 by default, adjustable per batch (7.5). |
| Duplicate sheets | Two or more sheets of a submission bound to the same page; the first in physical page order counts (5.7). |
| Linked copies | The copies of an object shown on all pages, which behave as one object (2.6). |
| Missing pages | Pages of the variant with no sheet in the submission (5.7). |
| Reading | What extraction read for one object on one sheet, and what review decided about it. |
| Reserved bands | The 15 mm bands along the edges of every page, where no object may lie (4.3). |
| Resolved | A reading that a reviewer has confirmed, replaced or flagged; undoing the review makes it unresolved again. |
| Respondent | The person who filled in a submission. |
| Respondent code | A `CODE` with the `RESPONDENT_IDENTIFIER` role (4.1). |
| Respondent ID | The identifier of a respondent read from a respondent-ID grid, a respondent code or a handwritten field. |
| Respondent-ID grid | A `MATRIX` with the `RESPONDENT_IDENTIFIER` role: one column per character, one row per possible value (4.1, 7.3). |
| Review required | A reading that needs a human decision (7.4). |
| Scan readiness | The condition under which a project accepts sheets: its published variants can be told apart (5.2). |
| Sheet | One scanned physical page. |
| Submission | The sheets filled in by one respondent on one variant, processed and scored as a whole (5.5). |
| Summary export / Detail export | The two results files of a batch: one row per submission, and the same with one column per question (10). |
| Unresolvable | The state of a submission whose variant or grouping a reviewer has declared impossible to determine (5.10). |
| Unresolved | A reading that needs review and has no review outcome yet, whether the submission is open or closed. |
| Validity | Whether the marks read respect the rule of the question: `VALID`, `NO_ANSWER`, `TOO_MANY_ANSWERS` (7.2). |
| Variant | One printable version of a form (2.3). |
| Variant code | A `CODE` with the `VARIANT_IDENTIFIER` role, printed on every page (4.1, 4.2). |
