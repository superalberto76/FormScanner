# 5. Printing, scanning and common problems

FormScanner reads the answers from scanned images. How well it reads depends on how the form is laid out, printed, filled in and scanned. This chapter collects the practical advice and the solutions to the problems users meet most often.

## Contents

1. [Designing a form that reads well](#51-designing-a-form-that-reads-well)
2. [Printing](#52-printing)
3. [Filling in](#53-filling-in)
4. [Scanning](#54-scanning)
5. [Files to upload](#55-files-to-upload)
6. [Common problems](#56-common-problems)

## 5.1 Designing a form that reads well

- **Keep objects out of the reserved bands.** A 15 mm band along each edge of the page holds the alignment marks; the editor draws it and does not let objects into it. Do not try to fill the page to the edge.
- **Give options enough room.** A question prints one 18-pixel row per option below its text; options that do not fit the height of the object are not printed. Check the preview after adding options.
- **Several variants need a variant code.** If the project has more than one published variant, put a QR code with the **Variant identifier** role on the first page and turn on **Show on all pages** *before* adding the other pages: pages that already exist do not receive the copy. Turn on **Include page number** too: every sheet then says which page it is, and the order of the scanned pages no longer matters.
- **Prefer QR codes to barcodes.** A long value in a narrow barcode may not be readable once printed and scanned. If you use a barcode, make it wide.
- **Plan how respondents are identified.** A respondent-ID grid (a matrix where the respondent marks one digit per column) is read automatically; a handwritten name is always checked by a reviewer; a pre-printed or stuck-on respondent code is the most reliable. See [4.4](04-reference.md#44-scanning-roles).
- **Use plain characters.** The PDF prints the letters of Western European languages; typographic quotes, dashes, the euro sign and other scripts print as `?` ([4.3](04-reference.md#text)).
- **Export the PDF and look at it** before publishing: what you see in the PDF is what is printed and what is read.

## 5.2 Printing

- **Print the "student" PDF at 100% scale** ("Actual size"), never "Fit to page" or "Shrink to fit". The alignment marks lie 8 mm from the edges of the sheet, inside the printable area of common printers at 100%; a printer that shrinks the page still works, but there is no reason to risk it.
- **A4 paper, portrait.** Every page of a form is A4 portrait.
- **One side per sheet** is the safe choice. Double-sided printing works if the scanner also scans both sides and every side is a page of the variant, in order.
- **Black on white.** The marks and the bubbles are black; colour or grey toner saving makes the bubbles fainter but they are still read.
- **Print from a published variant.** A published variant cannot change, so what you print is what FormScanner reads. To change a form after publishing, clone the variant (the project must be a draft), edit and publish the clone, and print again.
- **The "solution" PDF** has exactly the layout of the student one, with the correct answers filled in. It is a key for people, for example to lay over a sheet; FormScanner does not need it to compute the score.

## 5.3 Filling in

Tell the respondents, before they start:

- fill each bubble well, with a dark pen or pencil: a bubble counts as marked when it is filled at least by half;
- to change an answer, use a new sheet. Crossing out a bubble does not cancel it: a crossed-out bubble filled at least by half still counts as marked, and may be accepted without review. A pencil mark that is not fully erased may also be read as marked;
- write inside the boxes of the free-text areas, in capital letters if possible;
- do not write or draw on the black squares and bars along the edges of the sheet;
- do not fold the sheets across the corners.

## 5.4 Scanning

- **Resolution:** 150 dpi is enough. Every page of a PDF is converted to 150 dpi when it is uploaded, so a higher resolution does not improve the reading of a PDF, it only makes the file larger. Loose images are read as they arrive: 150 dpi is enough for them too, and a higher resolution may help with small barcodes and with handwriting.
- **Colour:** greyscale or colour. Avoid pure black-and-white (1-bit) scanning, which can turn a light mark into nothing or a shadow into a mark.
- **Whole sheet:** scan the full A4 page, with nothing cropped: the four corner marks must be in the image. Turn off any automatic cropping, deskewing or "blank page removal" that cuts margins.
- **Automatic document feeder:** load the sheets in portrait orientation, short edge first. Small skews (up to about 10°) and a stack inserted upside down are corrected automatically. Landscape feeding is not supported.
- **Flatbed:** place the sheet straight against the edges of the glass. On a glass exactly as tall as the sheet, a sheet rotated by more than a few degrees may lose a corner mark outside the image.
- **Phone photos** are not a supported input: FormScanner corrects shift, scale and small rotations, not the perspective of a photo taken at an angle.

## 5.5 Files to upload

- **One PDF per respondent** is the recommended input: the file itself says which sheets belong together, and the order of the pages in the file is the order of the pages of the form (unless the variant code carries the page number). Most scanners with a feeder can save each stack as a separate PDF.
- **Loose images** (JPEG or PNG, one sheet per image) are fully supported, but they need something to be grouped by: a respondent code printed on the sheet, or file names that follow the convention `<group>_page_<n>.<ext>`, for example `smith-john_page_1.jpg` and `smith-john_page_2.jpg`. An image that can be grouped neither way becomes a submission of its own in the **Unassigned** filter, which you can only mark unresolvable. The full rules are in the [specification, §5.5](../specification/functional-specification.md#55-grouping-sheets-into-submissions).
- **Limits:** up to 50 MB per PDF and 20 MB per image (an administrator can change the PDF limit, [1.9](01-installation.md#19-configuration)). Only `.pdf`, `.jpg`, `.jpeg` and `.png` files are taken.
- **More sheets later:** you can add files to a batch at any time while the project is active, for example when late sheets arrive. Loose images with the same group token as earlier ones join the same submission.

## 5.6 Common problems

| Problem | Cause and solution |
|---|---|
| The **Scanning** tab is locked, or **+ New batch** is missing | The project is not active. Publish a variant and **Activate** the project ([4.1](04-reference.md#41-projects)); a closed project accepts no new batches. |
| "Cannot start a new scan" with "No published variant exists for this project." | Publish at least one variant ([4.2](04-reference.md#42-variants-and-the-editor)). |
| "Cannot start a new scan": several published variants exist but none has a variant identifier | With more than one published variant, FormScanner needs a variant code to tell them apart. A published variant cannot be changed, unpublished or deleted: **Reopen** the project (cloning needs a draft project), clone the variants, add a QR code with the **Variant identifier** role to each clone, publish the clones, **Activate** the project again and print the clones. Sheets of the old variants, which carry no code, can still be uploaded: they wait in **To assign** for a reviewer to choose their variant. |
| Submissions in **To assign** with "No variant code readable" | The variant code was not read on any sheet of the submission: cropped, covered, smudged, too small. Use **Assign variant** and pick the variant by hand; then **Run extraction**. |
| Submissions in **To assign** with "Code … is not a published variant" | The sheets come from a variant that is not published, or from another project. Check that the right variant was published, and that the sheets belong to this project. |
| Submissions in **Unassigned** | Loose images that could not be grouped: no respondent code and a file name that does not follow `<group>_page_<n>`. Rename the files and upload them again, then **Mark unresolvable** the unassigned ones; or upload one PDF per respondent instead. |
| A sheet shows "Sheet N not aligned: readings may be shifted." | The corner marks were not found: cropped scan, a mark covered or torn, the sheet printed from a PDF without marks, or a strong rotation. Every reading of that page goes to review: check each one against **Show full sheet**. If many sheets are affected, check the scanner's cropping and the print scale. |
| Many readings in review with "Low confidence" | Faint marks (light pencil, toner saving) or a threshold too high for your sheets. Look at a few: if the readings are right, lower the batch's **Confidence threshold** ([4.8](04-reference.md#48-confidence-threshold)); readings already reviewed are not touched. |
| A submission stays in **To extract** after **Run extraction** | One of its pages could not be read (for example a damaged image). Run extraction again; if it still fails, use **Review anyway** and enter those answers from the paper sheet. |
| "Missing p. 2" on a submission | A page of the form was not in the upload. A PDF is always a submission of its own, so a missing page cannot be added to it: close the submission without the page, whose objects count 0 points. With loose images, upload the missing page with the same group token and run extraction again. |
| The export is disabled ("Not ready: …") | Some submissions are not closed yet. The message says how many, and why: to extract, in review, to assign, unassigned. Every submission must be closed or marked unresolvable, and at least one must be closed. |
| "This submission was changed by someone else. It has been reloaded." | Another reviewer saved on the same submission. Nothing is lost: the view shows the current state; repeat your last action if it is still needed. |
| A colleague is shown as reviewing a submission you want to work on | Use **Take over**. The colleague's view becomes read-only at their next save. |
| A question's options are cut off in the PDF | The object is not tall enough for all its options. Make it taller in the editor. |
| The "Max score" of the variant is 0 | No option has a score. The "correct" mark is used only by the solution PDF: give the options a score ([4.3](04-reference.md#43-object-types)). |
| The variant code is missing on some printed pages | **Show on all pages** was turned on after those pages were created: only pages added (or duplicated from a page that has the code) afterwards receive it. With one PDF per respondent, one sheet with a readable code is enough to match the whole submission. For the next form, place the code and turn on **Show on all pages** before adding the other pages. |
| Free-text readings are poor | The OCR suggestion is always checked by a reviewer. If the respondents write in another language, change the OCR language ([1.5](01-installation.md#15-ocr-language)). |
