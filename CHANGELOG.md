# Changelog

This skill's update log. Record what changed in each version published to MemHub Skill Store.

## 1.1.8 - 2026-11-02

1. The published package no longer ships a stray logs directory that an unrelated MCP server had written into the skill folder, and a .gitignore now keeps it and Python bytecode out

## 1.1.7 - 2026-11-02

1. Version is now single-sourced: the script reads it from the VERSION file or the CODEX_GF_VERSION injected by install.sh, instead of a hardcoded constant that could drift and make --check-update always think an update exists
2. The installer also drops a VERSION file into bin so runs from the installed copy resolve the right version
3. Uninstall removes that extra VERSION file too

## 1.1.6 - 2026-11-02

1. --update no longer drops into the full interactive install and hangs waiting for input under curl pipe bash; it now updates in place, keeping the existing main model and both fallbacks
2. README rewritten around three commands: install, update and uninstall, with the capacity and rate-limit rows added to the switch table

## 1.1.5 - 2026-11-02

1. Added --uninstall to remove the fallback mechanism and restore config.toml to its pre-first-install state, including the top-level model keys; providers created by the installer are deleted while pre-existing ones are kept
2. Added --check-update and --update so users no longer have to guess how to pick up fixes such as the capacity-error matcher
3. Update refuses to run from the installed local copy, which would just reinstall the old version, and points at the correct curl command instead
4. Uninstall works offline by preferring the local installer copy, and fails loudly instead of hanging when that copy is missing
5. A permanent first-install snapshot is now kept, because the rolling pre-install backups would eventually be evicted and make a true restore impossible
6. Fixed variable expansion being broken when a dollar sign was immediately followed by a full-width bracket in shell text

## 1.1.4 - 2026-10-30

1. Re-running the installer in auto mode now keeps the existing two fallbacks instead of silently replacing choices the user made earlier; only missing or delisted models are re-picked
2. Documented that upgrading is just re-running the same command, that it is idempotent, and that Codex must be restarted because config is only read at startup
3. Fixed a regex escape warning in the generated wrapper template

## 1.1.3 - 2026-10-30

1. Fallback now triggers on capacity and rate-limit errors, which Codex renders as plain text like "Selected model is at capacity. Please try a different model." instead of an unexpected status line, so these were previously treated as non-retryable and did not switch models
2. Also covers policy-restricted capacity, flex capacity, too many requests, overloaded and quota exceeded
3. The final fallback no longer claims it is about to switch to another model

## 1.1.2 - 2026-10-30

1. Fixed interactive menus accepting no input at all: opening /dev/tty in r+ mode raised UnsupportedOperation, which was swallowed as OSError and fell back to input() reading the piped script instead
2. Enter now keeps the current value as the menu promises, instead of being rejected as an invalid model name
3. Hidden token prompt also no longer depends on a seekable tty

## 1.1.1 - 2026-10-30

1. Fixed the installer appearing to do nothing under curl pipe bash, caused by redirecting bash stdin to the terminal so it read remaining script lines from the keyboard
2. Interactive prompts now read from the terminal inside python, so both piped and local runs work
3. Download failures now print the reason and a proxy hint instead of hanging silently, and retry four times
4. Removed exec when handing over to python so the temporary directory cleanup trap actually runs
5. The installer now leaves a stable copy in CODEX_HOME/bin so the printed restore command still works later

## 1.1.0 - 2026-10-30

1. Added public one line install: curl the installer from GitHub and pipe to bash, no manual download
2. Added auto mode that reads the current main model and picks two fallbacks automatically, so a machine that already has Codex configured needs zero input and zero token
3. Fixed interactive prompts being silently broken under curl pipe bash, where reads consumed the rest of the script instead of user input
4. Token is now resolved from the existing config first, then environment variables, and only asked for when the machine has no provider at all

## 1.0.1 - 2026-10-30

1. Fixed generated file headers still naming the old installer script, which caused duplicate comment lines to pile up on repeated installs
2. Both old and new header names are now recognized and cleaned, so reinstalling keeps exactly one managed header
3. Installer docstring now states model discovery order as gateway models endpoint first, then local catalog, then codex debug models

## 1.0.0 - 2026-10-30

1. Initial MemHub release: interactive installer that connects Codex to any OpenAI compatible gateway and writes provider config, main model, reasoning effort
2. Adds two level fallback through independent profile files fb and fb2, plus a codex-fb wrapper that retries only on retryable API and network errors
3. Discovers models from the gateway /models endpoint instead of codex debug models, which only lists Codex built in models
4. Probes reasoning effort with real requests and downgrades when the gateway rejects the declared level
5. Portable setup: copies to a new machine as a single script, creates CODEX_HOME when missing, never copies machine specific notify, projects or mcp absolute paths
6. Credits resolve in order from flag, environment variable, MemHub Skill Environment config, then interactive prompt, so no token is stored in the skill
7. Backup and one step at a time restore for config, both profiles and the wrapper
