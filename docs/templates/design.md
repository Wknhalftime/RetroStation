# <Topic> — design (YYYY-MM-DD)

**Status:** Draft | In review | Approved (Lance, YYYY-MM-DD) | Built (PR #…) | Obsolete
**Brief:** <link> · **Rulings proposed here:** AUD-R… · **SPEC:** <link, once written>

<!-- Write this only when building takes longer than writing it up, or when there is more than
     one sensible way to do it. Early and rough beats late and polished: Lance can react to a
     short draft, but has to unpick a finished one. Keep it under 150 lines; if it runs longer,
     split it. No code until the status says Approved. Delete these comments when done. -->

## Why

<!-- Two or three lines and a link to the brief. Do not restate the brief. -->

## Goals

## Non-goals

<!-- Nearby things this deliberately leaves alone. -->

## Anti-goals (a failure even if it "works")

## Options

| Option | What it gets us | What it costs | Verdict |
|---|---|---|---|
| (a) … | | | Chosen |
| (b) … | | | Rejected: … |
| Do nothing | | | |

<!-- List every option someone could reasonably propose. Costs include what an option does to
     existing rulings, to the one-worker queues (AUD-R017), to the layers and to the stream. -->

## Decision and reasoning

<!-- The choice and the data behind it: a measurement, a count, a profile or an incident, with
     a link. Say who decided. A decision that must outlive this feature becomes a ruling: draft
     it here with ruling.md. -->

## Design

<!-- Only what the SPEC cannot be written without:
     - data: migration, columns, and who writes and who reads each one
     - messages: Huey commands and their scope
     - API routes, and the UI states
     Copy an existing pattern (ARCHITECTURE.md rule 1). If it needs a new pattern, change
     ARCHITECTURE.md first. -->

## Constraints

<!-- Rulings and standards this must keep, cited by id. Limits it must stay inside. -->

## What could make this fail

<!-- Imagine it shipped and went wrong. List the likeliest reasons and what the design does
     about each: MusicBrainz down, a slow database, a message delivered twice, a file gone
     missing, an empty library, a restart halfway through. -->

## ARCHITECTURE.md changes

<!-- The sections this changes, or "none". The page and the code change in the same PR. -->

## Left to the builder

<!-- Choices Lance does not care about, so the builder does not have to ask. -->
