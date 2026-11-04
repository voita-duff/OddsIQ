---
tags:
  - component/{orchestration|scraper|engine|data|notification|cli|system}
  - status/{complete|in-progress|planned}
  - phase/{p0|p1|p2|p3|p4|p5|p6|future}
  - layer/{orchestration|data-ingestion|detection|tracking|storage|notification|operations|architecture|configuration}
  - type/component
  - status/template
source-files:
  - oddsiq/{path/to/module.py}
  - tests/test_{module}.py
related:
  - "[[Related Component 1]]"
  - "[[Related Component 2]]"
---

# {Component Name}

<!-- One-to-three sentences. What does this component do, and who calls it?
     Name the consumers by wikilink (usually [[Scheduler]] for orchestration,
     [[Edge Detection]] / [[CLV Calculation]] for engine consumers, etc.). -->

## Source Files

| File | Lines | Description |
|------|-------|-------------|
| `oddsiq/{path/to/module.py}` | 1-N | Full implementation |
| `oddsiq/{path/to/module.py}` | {start}-{end} | `{key_function}()` — {short purpose} |
| `tests/test_{module}.py` | — | {N} tests: {short area coverage} |

## Inputs

<!-- What does this component accept? List each with type, source, and short
     meaning. Distinguish constructor args / env vars / per-call args. -->

## Outputs

<!-- What does this component return / produce? List each with type and short
     meaning. Include side effects (DB writes, filesystem writes, network
     calls) if any. -->

## Interactions

<!-- Wikilinks to every other component this one talks to, with a short note
     on the relationship. Example:
     - [[Scheduler]] -- sole consumer; calls fetch_odds() on every poll cycle
     - [[Team Name Normalization]] -- normalizes bookmaker team names to
       canonical form before returning snapshots
-->

## Internal Logic

<!-- Full technical depth. Include code blocks for non-obvious sections.
     Organize into subsections if the component has distinct responsibilities
     (parsing, rate-limiting, session management, etc.). -->

### {Subsection 1}

### {Subsection 2}

## Configuration

| Constant | Value | Source | Notes |
|----------|-------|--------|-------|
| `{CONSTANT_NAME}` | `{value}` | `config.py` or `env var` | {why this value} |

<!-- Link to [[Config Constants]] if any of the values are documented there. -->

## Error Handling

<!-- One bullet per failure mode the component handles:
     - What triggers it (HTTP status, parse error, etc.)
     - Where it's caught (file:line)
     - What happens next (returns [], raises, resets session, etc.)
     - Whether it increments a failure counter / triggers recovery -->

## Known Issues

<!-- Link out to Known Issues/ notes (Fixed / Mitigated / Open). If there are
     none, delete this section. -->

## Tests

<!-- `tests/test_{module}.py` covers:
     - {bullet list of what's tested}

     Mirror the phrasing from existing component docs for consistency. -->
