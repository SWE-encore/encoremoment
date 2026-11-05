# Encore Moment

CSE406 Software Engineering · Group Encore · Hanyang University

Encore Moment is a PC-based audience reaction capture project. A team-controlled player and an AWS inference server support reaction detection, a live timeline, and locally saved audience clips. This repository currently contains project documentation; the specification describes planned behavior.

## Submission A

The revised proposal and requirements document is preserved separately in IEEE LaTeX format (4 pages). It retains the original smart-TV scope, role table, 22 requirements/objectives, and 27 references; Submission B contains the later PC-first specification.

- [Read the Submission A PDF](encore_moment_submission_a.pdf)
- [Browse the Submission A LaTeX source](submission-a/)
- [Download the Submission A LaTeX package](encore-moment-submission-a-latex.zip)

Build with `cd submission-a` followed by `latexmk -pdf main.tex`. The source README records the original document used for this transcription.

## Submission B

Updated 2026-11-06. The current PDF is 9 pages in IEEE conference format.

- [Read the PDF](encore_moment_submission_b_.pdf)
- [Browse the LaTeX source](submission-b/)
- [Download the complete LaTeX package](encore-moment-submission-b-latex.zip)

The Introduction contains Motivation, User and Client Needs, Related Software, and Team Responsibilities and Roles, including the original role-assignment table adapted to the PC client. Sections II–IV preserve the supplied revised B: Requirements, Development Environment, and Specifications. Requirements 1–11 are Must-Have, and 12–13 are Good-to-Have.

## Build the document

With a LaTeX distribution and `latexmk` installed:

```sh
cd submission-b
latexmk -pdf main.tex
```

Alternatively, upload the complete ZIP to Overleaf and select `submission-b/main.tex` as the main document. The package includes the IEEEtran class/style, bibliography, and all three PDF figures/tables.

GitHub publication is separate from the course LMS submission and printed copy.
