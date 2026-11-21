# outline

> Cover, 6 sections, appendix. Filled as each gate passes.

_TODO_

## Evaluation: mismatch table rules
- The G1 mismatch table uses validate_replay's categories (analysis/validate_replay.py CATEGORIES), then applies
  report/mismatch_reclassifications.csv row by row (each row: tx, from, to, evidence, who decided). Every applied
  reclassification is listed in a footnote under the table.
- Applied so far: held66 tx 0x2f374edb (chunk 1227695728-1227695937, block 1227695887), "real bug (unexplained)"
  -> "phantom/margin ALOs" (Gate 3 bbo evidence). With it, no unexplained engine bug remains in the table.
