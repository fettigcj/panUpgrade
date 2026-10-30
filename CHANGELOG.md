# Changelog

## Unreleased
- `select` form: **Save**, **Save & Exit** and **Quit without saving** buttons at the bottom, with an "unsaved changes" marker. Ctrl-D and Esc remain as shortcuts.
- `select` form: a "Filters in effect" line lists every active filter, including ones hidden because the column was switched to another mode; more space before the Included table.
- The list `select` saves defaults to `upgradeList.txt`. `plan`, `prepare` and `upgrade` use it when no firewalls are named (`--all` ignores it), and the form reopens on it. Without the form, `select` only writes a file when given `--out`.
- `select` form: Shift-Tab now goes backwards on Windows (the console key event is read directly, since `msvcrt` cannot see Shift). The serial, model, HA, device group and tag filters match anywhere in the value, and `*` / `?` work in them, on the command line too (`--model "PA-8*"`).
- Device groups were never read from Panorama (`show devices all` does not carry them), so the device group column was always empty. They now come from `show devicegroups`; device tags are read from `show devices all`.
- `select` opens a form on a terminal: filters sit above the table columns (with modes such as pattern / contains and is / older than / newer than), and Available and Included tables let you build the exact batch. The equivalent flags are printed afterwards. `-i` / `--no-interactive` control it.
- The fleet report no longer counts the PAN-DB URL version against an HA pair; only app, threat and av content are judged (the standby usually cannot reach the URL cloud).
- Sign-in follows your setup: one account, separate read and change accounts, or a login typed at the prompt (`--ask-credentials changes|all`). No account names are assumed; a file with a single account needs no flag.
- Licensed under GPL-3.0-or-later.
- `--panorama ADDRESS` replaces `--source panorama:ADDRESS` (`--source fake:FILE` remains for the invented fleet). Commands now say which Panorama, credentials file (shown under ~ when in the home folder, with a warning when it is not), account and settings file they use.
- New `report` verb (fleet state, no target) and `help` verb (command overview); options grouped into connect, choose-firewalls and appearance.
- Plan output grouped by verdict (blocked first), routine skipped checks counted instead of listed (`--verbose` lists them), identical step lists shared.
- Credentials path and account names are configurable (`PANUPGRADE_CREDS`, `PANUPGRADE_READ_ACCOUNT`, `PANUPGRADE_WORK_ACCOUNT`); upgrade kits pin them explicitly.
- Panorama TLS certificates are now verified by default; `--insecure-tls` / `PANUPGRADE_INSECURE_TLS=1` opts out with a warning.
- Safer defaults: `sync_to_peer = "prepare"`, `ha_config_unsynced = "block"`.
- Refused approvals say what changed; interrupted runs offer to continue or start fresh.
- Release-train minimum-content floors from the vendor release notes.
