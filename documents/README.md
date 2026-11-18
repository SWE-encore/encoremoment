# Submission documents

This directory contains the course submission sources and their published PDF and ZIP copies.
Application architecture and developer notes stay in [`../docs/`](../docs/); the executable PC/server message contract stays in [`../contracts/`](../contracts/).

| Submission | Scope | Editable source | Published PDF | Source archive |
|---|---|---|---|---|
| A | Original smart-TV proposal and requirements | [`submission-a/`](submission-a/) | [Submission A PDF](encore_moment_submission_a.pdf) | [Submission A ZIP](encore-moment-submission-a-latex.zip) |
| B | Later PC-first specification | [`submission-b/`](submission-b/) | [Submission B PDF](encore_moment_submission_b_.pdf) | [Submission B ZIP](encore-moment-submission-b-latex.zip) |

## Layout

```text
documents/
  README.md
  submission-a/                     LaTeX sources, IEEEtran class, and artwork
  submission-b/                     LaTeX sources, IEEEtran class/style, bibliography, and artwork
  encore_moment_submission_a.pdf
  encore_moment_submission_b_.pdf
  encore-moment-submission-a-latex.zip
  encore-moment-submission-b-latex.zip
```

Each source directory retains its internal layout, so the relative `sections/`, `images/`, and bibliography paths still resolve from that directory.

## Build locally

Install a LaTeX distribution that provides pdfLaTeX, BibTeX, and `latexmk`, together with the packages used in each `main.tex`.
Run either command sequence from the repository root:

```sh
cd documents/submission-a
latexmk -pdf main.tex
```

```sh
cd documents/submission-b
latexmk -pdf main.tex
```

Submission A includes its references as LaTeX text. Submission B uses BibTeX and includes the IEEEtran bibliography style.

## Published copies and Overleaf

The PDF and ZIP files were relocated without changing their bytes. They are published snapshots, not automatically regenerated outputs of the current source tree. In particular, a previously published PDF or source archive may retain the old repository path in its text.

The existing ZIPs retain `submission-a/` or `submission-b/` as their internal top-level folder. After uploading a ZIP to Overleaf, select `submission-a/main.tex` or `submission-b/main.tex`. If uploading the entire current repository instead, select `documents/submission-a/main.tex` or `documents/submission-b/main.tex`.

When publishing a revised submission, rebuild and review the PDF and refresh its source archive together. GitHub publication is separate from the course LMS submission and printed copy.
