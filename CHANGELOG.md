# Changelog

## Unreleased
- Licensed under GPL-3.0-or-later.
- `--panorama ADDRESS` replaces `--source panorama:ADDRESS` (`--source fake:FILE` remains for the invented fleet). Commands now say which Panorama, credentials file (shown under ~ when in the home folder, with a warning when it is not), account and settings file they use.
- New `report` verb (fleet state, no target) and `help` verb (command overview); options grouped into connect, choose-firewalls and appearance.
- Plan output grouped by verdict (blocked first), routine skipped checks counted instead of listed (`--verbose` lists them), identical step lists shared.
- Credentials path and account names are configurable (`PANUPGRADE_CREDS`, `PANUPGRADE_READ_ACCOUNT`, `PANUPGRADE_WORK_ACCOUNT`); upgrade kits pin them explicitly.
- Panorama TLS certificates are now verified by default; `--insecure-tls` / `PANUPGRADE_INSECURE_TLS=1` opts out with a warning.
- Safer defaults: `sync_to_peer = "prepare"`, `ha_config_unsynced = "block"`.
- Refused approvals say what changed; interrupted runs offer to continue or start fresh.
- Release-train minimum-content floors from the vendor release notes.
