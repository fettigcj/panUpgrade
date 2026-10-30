# Running panUpgrade

The whole workflow on one page. Commands are PowerShell; on Linux or macOS use the same arguments in your shell.

## Before you start

Work from a folder of your own, not the checkout. Runs, backups and the upgrade kit are written under the current folder.

```powershell
pip install -e <path to your panUpgrade checkout>
cd <your working folder>
```

**Credentials.** A file holding a dict of account to password. Point to it with `PANUPGRADE_CREDS` (or `--creds-file`).

| What | Flag | Environment variable | Default |
|---|---|---|---|
| Read-only account | `--account` | `PANUPGRADE_READ_ACCOUNT` | `panupgrade-read` |
| Account that makes changes | `--work-account` | `PANUPGRADE_WORK_ACCOUNT` | `panupgrade-write` |

Passwords are never printed or logged.

**TLS.** Panorama's certificate is verified. A lab with a self-signed certificate needs `--insecure-tls` (or `PANUPGRADE_INSECURE_TLS=1`); a warning is printed whenever checking is off.

## The workflow

### 0. Report on the fleet (changes nothing)

No target version needed. It reads every firewall Panorama manages (or the ones your criteria select) and says what needs attention, which PAN-OS versions are running, and how each firewall looks.

```powershell
panupgrade report --source panorama:<panorama-address>
```

### 1. Pick the firewalls (changes nothing)

Criteria combine. The output is a list you check and edit.

```powershell
panupgrade select --source panorama:<panorama-address> --version-is 10.2.1 --name-contains <text> --out firewalls.txt
```

Criteria: `--name-contains`, `--name` (glob), `--version-is` (glob), `--version-older-than`, `--version-newer-than`, `--ha-state`, `--model`, `--device-group`, `--tag`, `--serial`. Use `--serials-file firewalls.txt` in every command below.

### 2. Plan (changes nothing)

What would happen to each firewall, why some are blocked, and what needs your acknowledgement. The output lists blocked firewalls first and counts routine skipped checks instead of listing them (`--verbose` lists every check).

```powershell
panupgrade plan --source panorama:<panorama-address> --serials-file firewalls.txt --target 10.2.2 --settings settings.toml --xlsx readiness.xlsx
```

### 3. Prepare (optional, makes the window shorter and calmer)

Ahead of the window, `prepare` can:

- bring dynamic content (apps and threats, antivirus, WildFire) level between HA members. If it cannot, the config sync below is not attempted.
- force an active-to-passive config sync on pairs that read "not synchronized" (needs `sync_to_peer = "prepare"`; the passive is backed up first).
- with `--target`, pre-download the image, and any base image the path needs, onto each selected firewall.

It never installs, reboots or fails anything over. Whatever it cannot fix stays in the plan, which it re-runs at the end.

Dry run first, then the real thing. The real run shows exactly what you are approving and asks you to type APPROVE.

```powershell
panupgrade prepare --dry-run --source panorama:<panorama-address> --serials-file firewalls.txt --target 10.2.2 --settings settings.toml
```

```powershell
panupgrade prepare --source panorama:<panorama-address> --serials-file firewalls.txt --target 10.2.2 --settings settings.toml
```

When it finishes, `prepare` writes the **upgrade kit** into `prepared\` (change with `--kit-dir`):

- a sign-off pack (HTML, text and Excel)
- the firewall list and the approval
- a ready-to-run script, `run-upgrade-<target>-<time>.bat` on Windows or `.sh` elsewhere

There is no second prompt. Take the pack to whoever must agree, then come back to the script.

### 4. Upgrade

When the window opens and the people who must agree have, run the script. **Running it is the go-ahead.** It prints the cautions it proceeds on.

```powershell
prepared\run-upgrade-10.2.2-<time>.bat
```

The approval holds only while the plan is unchanged (the firewalls, their versions and HA roles, the planned steps and the cautions; not free disk, session counts or timestamps) and for `approval_max_age_hours` (default 168, a week). If an admin swapped HA roles in the meantime, or a new caution appeared, the script refuses, lists exactly what changed, and tells you to run `prepare` again.

To rehearse the upgrade against a model of the fleet (asks nothing, changes nothing):

```powershell
panupgrade upgrade --dry-run --source panorama:<panorama-address> --serials-file firewalls.txt --target 10.2.2 --settings settings.toml
```

You can also skip `prepare` and run `upgrade` without `--dry-run`. It then shows the plan and asks you to type APPROVE.

## Journals and resuming

Every run keeps a journal of what it did. A new time-stamped file is created under `runs\` and its path is printed at the start. Nothing is overwritten: the journal is the record of what was done and what makes a half-finished run safe to continue.

If an `upgrade` of exactly the same plan did not finish, the tool says so and asks:

- **CONTINUE**: carry on. Finished steps are skipped. A step that was started but not finished is reported for you to check and never repeated. A failed firewall is not retried automatically.
- **NEW**: start a fresh run.

Without a terminal, add `--resume` or `--fresh`. `--journal FILE` and `--runs-dir DIR` override the defaults. Config backups land in the `backup_dir` setting before any install or reboot. `prepare` is safe to repeat: it checks the firewalls themselves.

## Approving

One requirement: a person decided to go ahead knowing what will change and which cautions apply.

- **Kit flow:** the decision is running the script, after the pack has been read and agreed.
- **`prepare`'s own tasks** (config sync, image downloads) are confirmed by typing APPROVE, because they change firewalls straight away.
- **`upgrade` run directly** shows the plan and asks you to type APPROVE.

Every approval records who and when, names exactly the firewalls listed, and stops working if the plan underneath it changes or after `approval_max_age_hours`.

## Tweaking behaviour

`settings.example.toml` lists every setting with its default. Change one run with `--set name=value`, or keep a file and pass `--settings FILE`. Loosening a safety setting (`sync_to_peer`, `ha_config_unsynced`, `allow_downgrade`, wide session bands, `max_parallel`) prints a caution.

## What to confirm on your first live run

These were observed in one lab only, so watch them on yours: the reply shape of the download and install jobs, how a rebooting firewall looks through Panorama, and whether `sync-to-remote` returns before or after the sync finishes (the tool polls rather than assuming). Anything it cannot read stops the step and puts the device's own words in the journal.
