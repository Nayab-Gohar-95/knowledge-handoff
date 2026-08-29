# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## Project

IBM Watsonx hackathon starter template for the **Knowledge Continuity Suite** project.
No application code exists yet — `agents/` and `outputs/` are empty placeholder directories.

## Repository Structure

| Path | Purpose |
|------|---------|
| `agents/` | Empty — intended for agent scripts/definitions |
| `outputs/` | Empty — intended for agent-generated output |
| `sample-repos/` | Test data only (cloned external repos) — **do not document or analyse these as project code** |
| `.bobignore` | Prevents Bob from reading files matching credential patterns |
| `.env.example` | Template for credentials — **never readable by Bob** (matches `.bobignore`) |

## Critical Security Constraints

- **Never read `.env`** — Bob cannot access it (blocked by `.bobignore`); this is intentional.
- **`.gitignore` blocks `*token*`, `*secret*`, `*password*`, `*credentials*`** — any file whose name matches these patterns will not be committed; do not name files with these words.
- **`.bobignore` also blocks `*config.json` and `*config.yaml`** — avoid naming config files with these exact suffixes if they need to be readable by Bob.
- Credentials must always go in `.env` (environment variables), never hardcoded. Use `process.env.VAR` (JS), `os.getenv('VAR')` (Python), or `System.getenv("VAR")` (Java).

## No Build System Yet

There are no package managers, build tools, test frameworks, or lint configs defined.
When the project stack is chosen, update this file with build/test/lint commands.
