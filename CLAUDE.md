# Working in this repo - standing instructions

Read this first, every session.

## Git
- **Commit after each logical chunk of work** as it lands - a new module, a real bug fix, a
  documented decision - without waiting to be asked. This project wants a real, granular commit
  history to review and revert against, not one giant diff at the end. This is a deliberate
  departure from a habit of "only commit when explicitly asked" used elsewhere - don't default
  back to that here.
- **Never push, and never create/touch the GitHub repo, without asking first - every time**, even
  though commits themselves are pre-authorized. This project is staged private-first; going
  public is a deliberate, separate decision the user makes explicitly, not something to infer
  from "the code looks ready."
- Prefer new commits over amending. Write commit messages that explain *why*, not just *what* -
  matching this project's own documentation habit (see `docs/ARCHITECTURE.md`).

## What never gets committed
- `config/*.json` except `config/config.example.json` - real per-title configs carry the user's
  actual file paths. If you create or edit a real config, confirm it's not `git add`-able before
  committing (`.gitignore` already covers this, but double-check after any broad `git add`).
- `history/` (the whole folder) - per-title investigation logs, ported from a prior project,
  full of real file paths and specifics about which source releases were tested. Useful locally,
  never meant to be public even after this repo itself goes public.
- `credentials.json` / `.env` - the Growatt API login and any other secrets.
- Before any commit that touches something outside `solare/`, `docs/`, or top-level
  project files (pyproject.toml, README, etc.), double-check `git status` for anything that
  looks like it strayed from `config/` or `history/` despite the ignore rules.

## Python: always `uv`, never system Python/pip
Never call `python`/`pip` directly. `uv run` / `uv sync` / `uv add` for everything - this machine
(and this project) standardizes on `uv` for all Python work.

## Platform-specific code stays physically separated
Windows-only code (the MSI Afterburner shared-memory sensor reader, `NtSuspendProcess`-based
process suspend) lives in `solare/platform/windows/`, not behind inline
`if platform.system()` checks scattered through the codebase. `solare/platform/linux/` is
the equivalent home for the Linux implementations landing once the user's own machine switches
OS. Everything else in the codebase depends only on the common interface `platform/__init__.py`
exposes, never on which concrete implementation is active.

## Documentation
`docs/` is committed and stays **strictly about this tool's own architecture** - standing design
facts go in `docs/ARCHITECTURE.md`, stated plainly as current facts, not as a narrated journey
("we tried X, then hit a bug, so we switched to Y"). Keep it neutral: no personal file paths, no
specifics about which copyrighted source files were used for testing, and no encoding-domain
wisdom (CRF/psy-rd tuning, codec-specific quirks) even when it was a real, hard-won lesson - this
project is a general-purpose orchestrator, not a record of one person's encoding decisions or
development history, and mixing either in would make the public repo read as that instead.

Bugs/lessons learned, and any backstory behind *why* a decision was made (not just what it is),
go in `history/pitfalls.md` instead - never committed, but still worth writing down for your own
future reference, with the mechanism and the fix, not just "fixed a bug." When genuinely
uncertain whether something belongs in committed `docs/` or private `history/`, default to
`history/` and ask rather than guess.
