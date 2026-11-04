---
tags:
  - type/issue
  - status/{fixed|mitigated|open}
  - status/template
  - phase/{p0|p1|p2|p3|p4|p5|p6|future}
related:
  - "[[Related Component]]"
section: "{N.N}"
---

# {Fixed|Mitigated|Open}: {Short Title}

## Status

<!-- One line: "Fixed." / "Mitigated." / "Open (latent — no current impact)." /
     "Open (by design — {phase} feature)." etc.
     Add a parenthetical only if it needs context. -->

## Location

<!-- File + line range for the code the issue lives in, or the component doc
     that describes the affected behavior. Prefer function-name references
     over line numbers when code is stable; line numbers rot.

     Example: `engine/clv.py::resolve_bet_result` (around lines 12-16)
-->

```python
# Short relevant code snippet — the minimum needed to understand the issue.
# Omit this block for issues that are process-level rather than code-level.
```

## Problem

<!-- What goes wrong? Describe the observable symptom (wrong output, crash,
     silent failure, stale data, etc.) and when it surfaces. -->

## Root Cause

<!-- Why does the problem happen? Link to related components. -->

## Fix Applied

<!-- For "Fixed" and "Mitigated" entries: describe the change, link to the
     commit hash if relevant, and mention any new tests.
     For "Open" entries: write "None." or describe the planned fix and its
     blocker. -->

## Remaining Risk

<!-- For Fixed: "None." is the common answer. If the fix doesn't cover every
     edge case, name the gap.
     For Mitigated: describe the residual risk — what failure mode is still
     possible, how likely, how severe.
     For Open: describe who/what is currently impacted and the conditions
     that would make the latent bug manifest. -->
