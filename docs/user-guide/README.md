# FormScanner user guide

FormScanner is a self-hosted web application for paper-based exams and questionnaires: you design the form in the browser and print it, the respondents fill it in by hand, and FormScanner reads the scanned sheets, asks a person about what it is not sure of, computes the scores and exports the results.

This guide is for the people who install FormScanner and for the people who use it: teachers, examiners, survey staff. It needs no technical knowledge beyond installing Docker, which only the administrator does.

## Chapters

1. [Installation and first start](01-installation.md): install with Docker Compose, sign in as the first administrator, access from other machines, OCR language, updates, backups, configuration.
2. [Concepts](02-concepts.md): projects, variants, pages and objects; batches, submissions and readings; review, score and results.
3. [Walkthrough](03-walkthrough.md): a complete job, step by step, from an empty project to the exported results.
4. [Reference](04-reference.md): every function, screen by screen: projects and members, the editor and the object types, scanning roles, batches, extraction, review, the confidence threshold, the export, accounts.
5. [Printing, scanning and common problems](05-printing-and-scanning.md): how to design, print, fill in and scan forms that read well, and what to do when something goes wrong.

New to FormScanner? Read chapter 2, then follow chapter 3 on your own installation.

## Other documents

- The [functional specification](../specification/functional-specification.md) states every rule FormScanner applies, with the reasons behind it. This guide links to it where the details matter.
- The [technical documentation](../specification/architecture.md) describes how FormScanner is built, for administrators and contributors.
