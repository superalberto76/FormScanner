# 4. Reference

This chapter describes every function of FormScanner, screen by screen. [Chapter 3](03-walkthrough.md) shows the same functions in the order of a real job; this chapter is where you look something up. The rules behind each function are in the [functional specification](../specification/functional-specification.md), linked where they matter.

## Contents

1. [Projects](#41-projects)
2. [Variants and the editor](#42-variants-and-the-editor)
3. [Object types](#43-object-types)
4. [Scanning roles](#44-scanning-roles)
5. [Batches and upload](#45-batches-and-upload)
6. [The batch page: extraction and queue](#46-the-batch-page-extraction-and-queue)
7. [Review](#47-review)
8. [Confidence threshold](#48-confidence-threshold)
9. [Results export](#49-results-export)
10. [Your account](#410-your-account)
11. [User administration](#411-user-administration)

The top bar has the **FormScanner** link, which goes back to the list of projects, and, on the right, your avatar: it opens the menu with **Profile**, **Appearance**, **Sign out** and, for administrators, **Manage users** and **Add user**.

## 4.1 Projects

### The list of projects

**Projects** lists the projects you are a member of (all of them, for an administrator), with their type, event date, number of variants, status and members. Click a row to open the project.

**+ New project** opens the form: **Type** (EXAM, SURVEY, QUESTIONNAIRE, GENERIC_FORM; informative only), **Name** (required), **Description**, **Event date**. **Create project** creates it, with you as its owner.

### Members

The **Members** column shows the avatars of the members. Click them to open **Project members**, with the owners and the collaborators.

An owner (or an administrator) can:

- **Add collaborators**: search a user by name or email (at least 2 characters) and click **Add**;
- **Make owner**: promote a collaborator;
- **Remove owner**: demote an owner to collaborator, after a confirmation;
- 🗑 **Remove from the project**: remove a collaborator, who loses access.

There must always be at least one owner, and you cannot remove or demote yourself.

### The project page

The project page shows the type, the event date and the description, and has two tabs: **Variants** ([4.2](#42-variants-and-the-editor)) and **Scanning** ([4.5](#45-batches-and-upload)). **Scanning** is locked while the project is a draft.

Next to the status, owners and administrators see the buttons that change it:

| Status | Buttons | Effect |
|---|---|---|
| DRAFT | **Activate**, **Close** | **Activate** needs at least one published variant. While the project is active, the variants cannot be edited. |
| ACTIVE | **Close**, **Reopen** | **Close** ends the collection: no new batches or uploads; extraction, review and export stay available. **Reopen** brings the project back to draft. |
| CLOSED | **Reopen**, **Archive** | **Archive** is final: it cannot be undone. The variants and the members can no longer be changed and no batch can be created; extraction, review and export of the existing batches stay available. |
| ARCHIVED | — | |

Each change asks for a confirmation. Reopening keeps the published variants published.

**Delete project** deletes the project with its variants and objects. It is available only while every variant is a draft: once a variant has been published, the project can no longer be deleted.

## 4.2 Variants and the editor

### The list of variants

The **Variants** tab lists the variants with their name, root key, version, number of pages and status. Click a row to open the variant in the editor.

**+ New variant** asks for a **Name** (required), a **Description** and a **Root key**: a short identifier used in the name of the exported PDFs (`variant-1`, `variant-2`… if left blank). **Create variant** creates the variant with one blank page and opens it.

**Delete** removes a draft variant with all its objects, after a confirmation. Published variants cannot be deleted.

### The editor

The editor shows, from left to right, the list of pages, the page, and a panel with the identification summary, the objects of the page and the details of the selected object.

The editor can change the variant only while both the variant and the project are drafts. Otherwise a banner says why it is read-only; you can still look at every object, switch the preview and download the PDFs.

**The variant bar**, at the top, shows the name, the status, the root key, the version, the description and **Max score**: the highest score a submission of this variant can reach ([specification, §9.6](../specification/functional-specification.md#96-maximum-score)). Its buttons:

| Button | What it does |
|---|---|
| **Edit** | Changes the name and the description (draft only). |
| **Publish** | Publishes the variant, after a confirmation. Allowed while the project is a draft or active. Publishing is refused, with the reason, if an object lies in a reserved band or if another published variant of the project has the same variant code. |
| **Clone** | Creates a new draft copy of the variant and opens it. Allowed only while the project is a draft. The clone does not keep the scanning roles: assign them again ([4.4](#44-scanning-roles)). |
| **Download Student PDF** | The sheet to print and fill in, with empty bubbles. |
| **Download Solution PDF** | The same sheet with the correct options and cells filled in. |
| **Delete** | Deletes the draft variant. |

The PDFs can be downloaded in any status, also from a draft, to check the layout.

**Preview: Student / Solution** switches the page between the two versions: **Solution** fills the bubbles of the correct answers, as the solution PDF.

### Pages

The **Pages** column lists the pages. On a draft:

- **+ Add page** adds a blank A4 page at the end. Objects shown on all pages are copied onto it.
- ⧉ **Duplicate page** adds a copy of the page right after it.
- ⠿ drag a page to change the order.
- 🗑 **Remove page** removes the page and its objects, after a confirmation. Objects shown on all pages lose only the copy on this page. A variant always keeps at least one page.

### Drawing and arranging objects

- **Add object**: choose a type (Text, Code, Free text, Question, Matrix, Image), then drag a rectangle on the page. When you release the mouse, a form asks for the content of the object ([4.3](#43-object-types)). To leave without drawing, click **Select / Edit**.
- **Select**: click an object on the page or in the **Objects** list.
- **Move**: drag the selected object. **Resize**: drag one of its corner handles. Changes of position and size are saved at once.
- Objects cannot enter the reserved bands, drawn as dashed lines along the edges together with the alignment marks. An object that is already inside a band (for example after a change of layout) shows a **!**: "Inside the reserved band: move it inside the page area." A variant with such objects cannot be published.
- **Sort objects**: **Bring to front**, **Bring forward**, **Send backward**, **Send to back** change which object lies above which.
- **Snap**: **Snap to grid** and **Snap to object** help align objects while moving and resizing.

The **Objects** list shows the objects of the page from front to back. Its tags: **hidden**, 🔒 locked, 🔗 shown on all pages, 🏷 holds a scanning role, **!** inside a reserved band.

### Editing an object

Selecting an object opens its panel. Changes to the content are kept until you press **Save**; **Reset** discards them. Leaving an object with unsaved changes asks whether to discard them.

The panel has these sections:

- **Shared across pages**:
  - **Show on all pages** copies the object onto every page added from now on. Pages that already exist do **not** receive a copy. The copies are linked: moving, resizing, reordering, editing or deleting one applies to all.
  - **Unlink from other pages** detaches the copies: each page keeps its own independent object.
  - **Relink to all pages** links the copies again. If other pages hold independent copies, you choose between **Reconnect & overwrite** (the other copies take this page's version) and **Create a new link** (the other copies stay independent).
- **Special role**: see [4.4](#44-scanning-roles).
- **Presentation**: **Label** (the name of the object in the lists and in review), **Description**, **Visible**, **Locked** (a locked object cannot be moved or resized).
- **Type configuration**: the content, by type ([4.3](#43-object-types)).

**Visible.** An object with **Visible** unticked stays in the variant but is not printed. It is still read from the scans and still counted in the maximum score, so a hidden question lowers every percentage: delete it instead of hiding it.

**Delete**, at the top of the panel, deletes the object after a confirmation; for an object shown on all pages, it deletes every copy. An object with a scanning role cannot be deleted until the role is removed.

**ⓘ Technical data** shows the position and size of the object.

## 4.3 Object types

Every object has a **label**, set when you create it and changed in **Presentation**. Choose labels you will recognise: the label is what a reviewer sees.

### Text

Printed text. **Text**, **Font size**, **Bold**, **Italic**; in the panel also **Font family**: a name containing "Times" or "serif" prints in a serif font, "Courier" or "mono" in a monospaced font, anything else in a sans-serif font. Text that does not fit its box is printed smaller.

**Characters.** The PDF prints the letters of Western European languages (accented letters included). Other characters print as `?`: typographic quotes and apostrophes (“ ” ’), dashes (– —), the euro sign, the ellipsis (…), Greek, Cyrillic or Asian scripts. Text pasted from a word processor often contains typographic quotes: type plain ones (`"` `'`) and hyphens instead. This applies to every printed text: questions, options, labels, codes.

### Image

A picture: PNG, JPG, BMP or SVG, up to 10 MB. **Fit**: **Contain** shows the whole picture inside the box, **Cover** fills the box and may cut the picture. **Replace image** changes it.

### Code

A QR code or a barcode. **Code type**: QR or BARCODE; for a barcode, **Encoding**: CODE128, CODE39, EAN13, EAN8 or UPCA. **Value**: what the code contains; FormScanner checks that the value suits the encoding. **Human readable** prints the value under the code.

A code without a role is read from the scans and its value is exported in the detail export. With a role it identifies the variant or the respondent ([4.4](#44-scanning-roles)). **Include page number** is available on the variant code only.

Prefer QR codes: a long value in a narrow barcode may not be readable once printed and scanned.

### Free text

A box where the respondent writes by hand. **Placeholder**: a hint printed in light italics inside the box. **Max length** in the panel. The label is printed in bold above the box.

The handwriting is read by OCR as a suggestion and always checked by a reviewer. A free-text box without a role is exported as text in the detail export; it is not scored.

### Question

A question with its options, each with a bubble.

- **Question type**: SINGLE_CHOICE (one answer) or MULTIPLE_CHOICE (any number).
- **Question text**: printed in bold above the options.
- **Options**: the text of each option, ✓ to mark it correct, and in the panel its **score**. **+ Add option** adds one; **Remove** removes one (saved with **Save**). On a single-choice question only one option can be correct.
- **Shuffle answers**: the setting is saved, but the printed PDF does not shuffle the options: they are always printed in the order of the list. To have versions with a different order, create one variant per order.

**Scores.** The score of an option can be positive, zero or negative, with decimals. An option without a score is worth 0, even if it is marked correct: the ✓ is used by the solution PDF, not by the score. Give at least the correct options a score, and check **Max score** in the variant bar.

The question prints one row per option below its text. Options that do not fit the height of the object are not printed: make the object taller.

How a question is scored, including two bubbles on a single-choice question or every option marked, is in the [specification, §9.2](../specification/functional-specification.md#92-scoring-a-question).

### Matrix

A grid of bubbles with labelled rows and columns: a table of statements to rate, a set of true/false questions, or a respondent-ID grid ([4.4](#44-scanning-roles)). It is created in two steps:

1. **Structure**: **Rows** and **Columns**, **Selection rule**, **Evaluative**, **Row labels** and **Column labels**.
2. **Cell definitions** (optional): for each cell a **score** and **correct**. With a one-per-row or one-per-column rule, only one cell per row or column can be correct.

Selection rules:

| Rule | Meaning |
|---|---|
| ONE_PER_ROW | One mark in each row; each row is answered separately |
| MANY_PER_ROW | Any number of marks in each row |
| ONE_PER_COLUMN | One mark in each column; each column is answered separately |
| MANY_PER_COLUMN | Any number of marks in each column |
| FREE | Any marks anywhere; each row is answered separately |

Only a matrix marked **Evaluative** counts for the score: each row (each column, with a per-column rule) is scored as a question. A matrix that is not evaluative is read and exported, not scored. In the panel, **Edit cells** changes scores and correct cells; **Apply** keeps the changes until you press **Save**.

## 4.4 Scanning roles

The **Special role** section of an object gives it one of two roles. The **Identification** box, above the list of objects, shows which object holds each role in the variant.

| Role | Allowed on | What it does |
|---|---|---|
| **Variant identifier** | Code | Tells which variant a sheet is. Needed only when the project has more than one published variant. |
| **Respondent identifier** | Code, Matrix, Free text | Tells who filled in the submission: its value becomes the **candidate** in review and in the results. |

- **Set as Variant identifier** / **Set as Respondent identifier** assign the role; **Remove role** removes it.
- Each role can be held by one object per variant, and each object holds at most one role.
- **Variant identifier** requires the code to be shown on all pages ("Requires 'Show on all pages' enabled"). Because **Show on all pages** copies the object only onto pages added afterwards, **place the variant code on the first page and turn on Show on all pages before adding the other pages**.
- Each published variant needs a different value in its variant code: publishing a variant whose code value is already used by another published variant is refused.
- **Include page number** (on the variant code): every page prints the variant code together with its page number, so each sheet says which page it is, whatever the order of the scanned files. Recommended.
- A barcode can hold a role only with the CODE128 encoding; a QR code always can.
- Codes with a role print a short tag before their value (`VID:` or `RID:`), so FormScanner recognises them on the sheet. A respondent code made outside FormScanner, for example a sticker, must carry the same tag: `RID:` followed by the identifier.

**Respondent-ID grid.** A matrix with the **Respondent identifier** role is read as an identifier, not as a question: each column is one character, and the character is the label of the row marked in that column. For a student number of 6 digits, make a matrix with 10 rows labelled 0 to 9 and 6 columns, with the rule ONE_PER_COLUMN. Row labels cannot be empty and cannot contain `?`. A complete and clear grid sets the candidate without a review; an incomplete or ambiguous one goes to review.

**Handwritten identifier.** A free-text box with the **Respondent identifier** role (for the name, for example) is always checked by a reviewer, who confirms or corrects the OCR suggestion: that becomes the candidate.

The full rules are in the [specification, chapter 4](../specification/functional-specification.md#4-authoring-rules-for-scanning).

## 4.5 Batches and upload

### Scanning tab

The **Scanning** tab of the project lists the batches, with the date of creation, the number of submissions, a bar of how many are reviewed, how many readings are still to review, and a status: **No submissions**, **In progress** or **Ready for export**, with the counts of submissions to assign, unassigned or unresolvable. Click a row to open the batch.

**+ New batch** asks for a **Name** (required) and a **Description**, and opens the new batch. New batches can be created, and sheets uploaded, only while the project is **active** and ready for scanning. If it is not ready, a banner says why ("Cannot start a new scan") and **Go to Variants** takes you where it can be fixed. The reasons:

- "No published variant exists for this project."
- several published variants, none with a variant identifier;
- several published variants with the same variant identifier value.

### Uploading

On an empty batch the upload area is open; afterwards **+ Add files** opens it. Drag files onto it, or click it to choose them. Several files can be chosen at once.

- **PDF** files, up to 50 MB each: **each PDF becomes one submission**, whatever its number of pages. This is the recommended input: one PDF per respondent.
- **JPEG and PNG** images, up to 20 MB each, one sheet per image. Images are grouped into submissions by the respondent code printed on them or, failing that, by their file name: `<submission>_page_<n>.<ext>`, for example `smith-john_page_1.jpg` and `smith-john_page_2.jpg`. An image that can be grouped neither way becomes a submission of its own, **Unassigned**. The rules are in the [specification, §5.5](../specification/functional-specification.md#55-grouping-sheets-into-submissions).
- Other files are ignored, with a message.

**Upload N file(s)** sends them. Each PDF shows its outcome ("✓ Submission created", or the error with **Retry**); the images show a summary of the new, updated, unassigned and rejected submissions. A large selection shows a warning: it can take a while.

At upload, FormScanner reads the codes on each sheet, recognises the variant of each submission and binds every sheet to its page. It does not read the answers yet: that is [extraction](#46-the-batch-page-extraction-and-queue).

Files can be added to a batch at any time while the project is active.

## 4.6 The batch page: extraction and queue

The batch page is where the work of a batch happens: extraction, the threshold, the export and the list of submissions. **↻ Refresh** reloads it: the page does not refresh by itself while others work.

### Run extraction

**Run extraction** reads the answers of every submission waiting for it (**To extract**). On a large batch it takes minutes: keep the page open. When it ends, the page shows what the run did ("Last run: N submissions processed · N results created · N ready for review") and how many errors there were.

Running it again is always safe: it reads only what has not been read yet and never changes a reading or a review already there. Run it again after assigning variants, after adding files, or after errors. Only one run at a time can work on a batch.

### The list of submissions

Each submission shows its short ID, the candidate, the status, the variant, the pages, the readings still to review, the time of upload and who is reviewing it (🔒). Warnings: "⚠ No candidate" when the variant has a respondent identifier and none was read; "⚠ Missing p. 2"; the number of objects still unread; on a closed submission, the readings left unresolved.

The filters, each with its count:

| Filter | Submissions |
|---|---|
| **All** | every submission |
| **To assign** | variant not recognised (MATCH_FAILED); the reason is shown under the status |
| **To extract** | waiting for **Run extraction** |
| **To review** | readings still to review |
| **Ready to close** | read, nothing left to review, not yet closed |
| **Reviewed** | closed |
| **Unresolvable** | set aside by a reviewer |
| **Unassigned** | loose images that could not be grouped |

Click a submission that is read or closed to open it in [review](#47-review). Actions on the other ones:

- **Assign variant** (To assign): choose the variant among the published ones. Tick several submissions to assign them together. They are then read at the next **Run extraction**.
- **Mark unresolvable** (To assign, Unassigned): set the submission aside, after a confirmation. It is never read or reviewed, it no longer holds back the export, and it appears in the export without a score. **Undo unresolvable**, from the **Unresolvable** filter, brings it back.
- **Review anyway**: for a submission that stays in **To extract** because some of its objects could not be read even after a new run. It opens the submission in review; the objects that could not be read wait for you to enter their answer from the paper sheet.

## 4.7 Review

The review page shows one submission: the list of readings in review on the left, the selected reading on the right.

### Header

The header shows the variant, the **Candidate**, the **Pages** (with the missing ones), **To review** ("N of M remaining") and the **Score** so far. **Edit** next to the candidate lets you type or correct it; emptying the field clears it.

### The list of readings

Only readings that need a person are listed, grouped by sheet ("Page 2 · sheet 2"). Each shows why it is there: **Low confidence**, **Invalid answer**, **Always reviewed** (handwriting), **Code not decoded**, **Ambiguous digits**, **Missing digits**, **Grid not readable**, **Could not be read**, **Sheet not aligned**. Readings already resolved stay in the list, marked ✓ confirmed, ✎ replaced or ⚑ flagged; you can go back to them at any time. If nothing needed review: "Nothing to review: every reading was accepted automatically."

A sheet that could not be straightened carries a warning ("Sheet N not aligned: readings may be shifted. Check each one against the full sheet."): all its readings are in review.

### The selected reading

The right side shows the scan around the object (**Show full sheet** shows the whole page), the **Proposed reading** (for a question, the options marked with their confidence, such as "A (96%)"), **Why it is here**, and the actions:

| Action | Key | Effect |
|---|---|---|
| **Confirm** | Enter | The reading is right. |
| **Replace…** | R | The reading is wrong: enter what the sheet shows. For a question, tick the options (letters A–Z toggle them); for a matrix, the cells; for text, an identifier or a code, type it. Ticking nothing means the respondent did not answer. |
| **Flag unresolvable** | F | The answer cannot be told even from the paper sheet. It counts as no answer. |
| **Undo** | U | Takes back the outcome of a resolved reading. |

After **Confirm**, **Replace** or **Flag**, the next reading to review is selected. ↓ or J and ↑ or K move between readings; Esc closes the editor or the note. Keys do nothing while you type in a field.

**Confirm** is not offered when there is nothing complete to confirm (a code not decoded, an incomplete identifier, an object that could not be read): replace or flag it.

**+ Add note** attaches a note to the outcome, visible to the other reviewers.

Confirming or replacing the respondent identifier sets the candidate of the submission.

### Accept all remaining

**Accept all remaining (N)** confirms, after a confirmation, every remaining reading that can safely be accepted without looking at it one by one: the ones in review only for a low confidence or an invalid answer. Handwriting, incomplete or ambiguous identifiers, codes not decoded, objects that could not be read and readings on sheets not aligned are left for you to review one by one.

### Closing and reopening

**Close submission** ends the review of the submission and saves its score. It is always a deliberate step, also when nothing is left to review. If readings are still unresolved, the candidate is missing or pages are missing, a confirmation lists them: unresolved readings count as no answer (0 points) and are counted as "Unreviewed objects" in the export. The submission becomes read-only.

**Reopen submission** brings a closed submission back into review, with every outcome kept.

**Next submission →** opens the next submission in the filter you came from that is ready for review and not taken by someone else.

### Working together

Opening a submission in review reserves it for you: the batch page shows 🔒 with your name. The reservation lasts while you work, and expires after 15 minutes without saving.

- If a colleague is working on it, you see "🔒 In review by …. You can look, but not make changes." and can **Take over**.
- If a colleague takes it over from you, your view becomes read-only and offers **Take back**.
- If someone saved on the submission after you opened it, your save is refused and the page reloads the current state: "This submission was changed by someone else. It has been reloaded."

How the lock and the version work in detail is in the [specification, §8.7](../specification/functional-specification.md#87-several-reviewers-on-one-submission).

## 4.8 Confidence threshold

**Confidence threshold**, in the batch page, is the confidence below which a reading of a question, a matrix or a respondent-ID grid goes to review: 70% unless changed ("system default"). **Edit** sets another value for this batch only ("custom"), from 0 to 100%; **Reset to system default** goes back.

- A **higher** threshold sends more readings to review: safer, more work.
- A **lower** threshold sends fewer: faster, but a misread mark is accepted unseen more often.

The change applies at once, to the readings not yet reviewed of the submissions still open. Readings already reviewed and closed submissions are never touched. Handwriting is always reviewed, whatever the threshold.

An administrator can change the system default for the whole installation (`DEFAULT_CONFIDENCE_THRESHOLD`, see the [configuration reference](../specification/architecture.md#82-read-by-the-backend)).

## 4.9 Results export

**Export summary…** and **Export detail…**, in the batch page, are available when every submission of the batch is closed or unresolvable, and at least one is closed. Until then they are disabled and say what is missing ("Not ready: 2 to extract · 5 in review · 1 to assign").

The dialog asks for:

- **Format**: **CSV — comma separated** (`.` as decimal mark), **CSV — semicolon separated** (`,` as decimal mark, for spreadsheets set to a decimal comma), **Excel workbook (.xlsx)**. The browser remembers the last choice.
- **File name**: proposed as `Summary_<batch>_<date and time>Z` or `Detail_…`, with the time in UTC.

**Download** saves the file where the browser saves downloads.

**Summary**: one row per submission, in upload order, with the columns Submission, Candidate, Variant, Points, Max points, Percentage, Status, Missing pages, Unreviewed objects, Batch and Scanned at (UTC). An unresolvable submission has its row with points, maximum and percentage empty.

**Detail**: the summary columns, followed by one group of columns per object, in reading order (page, then top to bottom, then left to right):

| Columns | For | Content |
|---|---|---|
| `D1`, `D1_pt`, `D2`, … | each question, and each row (or column, with a per-column rule) of an evaluative matrix | the answer (the letters of the options marked: "A, C") and its points |
| `M1`, `M2`, … | each row (or column) of a matrix that is not evaluative | the labels of the cells marked |
| `T1`, `T2`, … | each free-text box without a role | the text, once reviewed |
| `C1`, `C2`, … | each code without a role | its value |

`*` marks an object without an answer: not answered, flagged, never reviewed, or on a missing page. An empty cell means the column does not exist for that submission's variant.

The columns are aligned **by position**: if the variants of a batch have different questions in the same position, `D3` is a different question for each variant. The Excel file has a second sheet, **Columns**, that says what each column is in each variant.

The full description of every column is in the [specification, chapter 10](../specification/functional-specification.md#10-results-export).

## 4.10 Your account

**Profile** (in the menu of your avatar):

- **Upload photo** / **Remove**: your profile picture, PNG, JPEG, BMP or SVG up to 10 MB. Without one, your initials are shown.
- **First name**, **Last name**: shown to the other members. The **Email** is your login and cannot be changed.
- **Change password**: your **Current password**, then the **New password** twice.

Passwords must have at least 8 characters, with an uppercase letter, a lowercase letter, a digit and a special character; a checklist shows which rules are met.

**Appearance**: **Theme mode** **Light**, **Dark** or **Sync with system** (follows the setting of your device). The choice is saved with your account.

**Sign out** ends the session. A session also ends by itself after some hours (8, unless the administrator changed it): you are then asked to sign in again.

When an administrator has created your account or reset your password, the first sign-in asks you to choose a new password (**Set a new password**) before anything else.

## 4.11 User administration

Administrators only, from the menu of the avatar.

**Add user** creates an account: **First name**, **Last name**, **Email** and a **Temporary password** of at least 8 characters. Give the email and the temporary password to the person: at the first sign-in they choose their own password. New accounts are users (USER): the interface does not create administrators. The administrator account is the one set up at installation ([1.9](01-installation.md#19-configuration)).

**Manage users** lists the accounts, with a search by name or email. For each user account:

- **Disable**: the user can no longer sign in, after a confirmation. **Enable** gives access back.
- **Reset password**: sets a new temporary password; the user chooses a new one at the next sign-in.

Administrator accounts cannot be disabled or reset from here.
