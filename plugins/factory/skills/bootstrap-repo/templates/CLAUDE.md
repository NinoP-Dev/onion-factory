<!--
TEMPLATE from /factory:bootstrap-repo. Replace every <placeholder>, delete sections
that have nothing true to say, and delete this comment. Keep the finished file
under 200 lines: write only what a newcomer (person or agent) would get wrong
without being told. Do not restate what the code, README or linters already say.
-->
# <Project name>

## What this is

<One or two sentences: what the project does, who uses it, and what it is NOT.>

## Layout

- `<dir>/` — <what lives here>
- `<dir>/` — <what lives here>
- `<dir>/` — <generated / vendored; never edit by hand>

## Commands

- Install: `<command>`
- Run locally: `<command>`
- All checks (the same command CI runs; must pass before a PR): `<command>`
- One test: `<command> <path-or-name>`

## Conventions a newcomer gets wrong

- <Rule stated as an instruction, e.g. "Money is integer cents; never float.">
- <Rule>

## Gotchas

- <Thing that looks wrong but is intentional, and why.>
- <Environment or data quirk that breaks local runs.>

## Connection points with the core project

<!-- Keep this section only if the core project depends on this repo. List each
path whose behaviour the core relies on. REVIEW.md treats changes here as
Important, and CODEOWNERS routes them to the core owner. -->
- `<path or glob>` — <what the core project relies on: API, schema, event, config key>

## Reviews
Before any code review, read REVIEW.md.
