# Running panUpgrade

The whole workflow on one page. Commands are PowerShell; on Linux or macOS use the same arguments in your shell.

## Before you start

Work from a folder of your own, not the checkout. Runs, backups and the upgrade kit are written under the current folder.

```powershell
pip install -e <path to your panUpgrade checkout>
cd <your working folder>
```

**Signing in.** You choose how; panUpgrade assumes nothing about your accounts.

- **A credentials file** holds a dict of account name to either a password, or a ready-made API key written `{'api_key': 'KEY'}`, for example `{'person': 'their-password', 'service': {'api_key': 'KEY'}}`. A password account is logged in with each run; an API key account is used as it is. Point to it with `--creds-file` or `PANUPGRADE_CREDS`. If it holds one account, that account is used. If it holds several, name one with `--account` (or `PANUPGRADE_ACCOUNT`).
- **A separate account for changes:** `--work-account NAME` (or `PANUPGRADE_WORK_ACCOUNT`). Without it, the same account is used for everything.
- **Type a login instead:** `--ask-credentials changes` asks for a username and password only for the steps that change firewalls, and reading still uses the file. `--ask-credentials all` asks once and uses that login for everything, with no file needed. A typed login is never stored, printed or logged.

Every command says which Panorama, credentials file and account it is using before it connects, and whether changes will be made as the same account. Passwords and keys are never printed.

The upgrade script that `prepare` writes remembers your choice. If you typed a login for changes, the script asks for it again when you run it.

**TLS.** Panorama's certificate is verified. A lab with a self-signed certificate needs `--insecure-tls` (or `PANUPGRADE_INSECURE_TLS=1`); a warning is printed whenever checking is off.

## The workflow

### 0. Report on the fleet (changes nothing)

No target version needed. It reads every firewall Panorama manages (or the ones your criteria select) and says what needs attention, which PAN-OS versions are running, and how each firewall looks.

```powershell
panupgrade report --panorama <panorama-address>
```

### 1. Pick the firewalls (changes nothing)

Criteria combine. The output is a list you check and edit.

**On a terminal, the easy way is the form.** Run `select` with no filters and a form opens:

```powershell
panupgrade select --panorama <panorama-address>
```

The filters sit directly above the columns they filter, and two tables sit below them: **Available** (firewalls that match the filters) and **Included** (the batch you are building).

- **Tab / Shift-Tab** walk across the filters, then on to the Available table, the Included table and the save box. The arrow keys do the same job: Up and Down move between a filter's mode row, its input row and the tables.
- **Modes.** A column with more than one way to filter shows `< pattern >` or `< older than >` above its input. Up to the mode row, then Left / Right to change it. Hostname offers *pattern* (`site1-*`) and *contains*; PAN-OS offers *is*, *older than* and *newer than*. Serial, model, HA, device group and tag are all *contains*: type `VM` and every model with VM in it matches, or add your own `*` or `?` (`PA-8*`) to say exactly where. Several words, separated by commas, match any of them. PAN-OS *is* is exact unless you add a `*` (`10.2.*`).
- **Known values.** On an input, Left / Right fill in a value this fleet really has (a version, an HA state, a model, a device group, a tag).
- **Building the batch.** In Available, Space (or Enter) moves the firewall under the cursor into Included, and `A` moves every firewall listed. In Included, Space, Enter or `D` takes one back out, and `C` clears the list. Filters only change what Available shows; they never remove anything from Included.
- **Finishing.** Tab to the save box and then the three buttons at the bottom: **Save** writes the list now and keeps the form open, **Save & Exit** writes it and finishes, **Quit without saving** leaves at once. The shortcuts are Ctrl-D for Save & Exit and Esc for Quit (Esc asks a second time if you have unsaved changes). The save box is filled in with `upgradeList.txt`; change the name if you like, or clear it to save no file.

A `!` in the left margin marks a firewall that is offline. When the form closes, `select` prints the flags that make the same list. If you picked firewalls by hand, it prints their serials, or points to the saved file when there are many. `-i` opens the form even when you give some filters: a `--serials-file` or `--serial` list starts in Included, and the other filters fill in the filter row. `--no-interactive` never opens it.

**Filters in effect.** The row under the filters lists every filter that is narrowing Available, including one left in a mode you have since switched away from (for example *PAN-OS is 10.2.\** after you moved that column to *newer than*). Those are marked *hidden, other mode* in yellow, so a list that looks too short always has its reason on screen. Switch the column back to that mode to change or clear it.

**The saved list is the default.** `plan`, `prepare` and `upgrade` use `upgradeList.txt` from the folder you run them in whenever you name no firewalls (no `--serial`, `--serials-file` or filter flag), and they say so when they do. Use `--serials-file` for another list, or `--all` for every firewall Panorama manages. Running `select` again with the form starts from the list you saved last time. Without the form, add `--out upgradeList.txt` to keep a list.

Or give the filters as flags, which is what scripts do:

```powershell
panupgrade select --panorama <panorama-address> --version-is 10.2.1 --name-contains <text> --out upgradeList.txt
```

Criteria: `--name-contains`, `--name` (glob), `--version-is` (glob), `--version-older-than`, `--version-newer-than`, `--ha-state`, `--model`, `--device-group`, `--tag`, `--serial`. The saved `upgradeList.txt` is picked up by every command below; use `--serials-file` to name a different list.

### 2. Plan (changes nothing)

What would happen to each firewall, why some are blocked, and what needs your acknowledgement. The output lists blocked firewalls first and counts routine skipped checks instead of listing them (`--verbose` lists every check).

```powershell
panupgrade plan --panorama <panorama-address> --target 10.2.2 --settings settings.toml --xlsx readiness.xlsx
```

### 3. Prepare (optional, makes the window shorter and calmer)

Ahead of the window, `prepare` can:

- bring dynamic content (apps and threats, antivirus, WildFire) level between HA members. If it cannot, the config sync below is not attempted.
- force an active-to-passive config sync on pairs that read "not synchronized" (needs `sync_to_peer = "prepare"`; the passive is backed up first).
- with `--target`, pre-download the image, and any base image the path needs, onto each selected firewall.

It never installs, reboots or fails anything over. Whatever it cannot fix stays in the plan, which it re-runs at the end.

Dry run first, then the real thing. The real run shows exactly what you are approving and asks you to type APPROVE.

```powershell
panupgrade prepare --dry-run --panorama <panorama-address> --target 10.2.2 --settings settings.toml
```

```powershell
panupgrade prepare --panorama <panorama-address> --target 10.2.2 --settings settings.toml
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
panupgrade upgrade --dry-run --panorama <panorama-address> --target 10.2.2 --settings settings.toml
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

On a terminal, run:

```powershell
panupgrade settings
```

Every setting is on one screen, grouped (HA behaviour, Panorama range, Failure handling, Downgrades, Images/disk/time). The column next to each shows its default, and a `*` marks one you have changed.

- **Up / Down / Tab** move between settings. **Left / Right** step through a choice (`sync_to_peer`: never, prepare, upgrade) or flip a yes/no. Numbers and text are typed.
- **Ctrl-R** (or Delete) puts a setting back to its default.
- The bottom of the screen says what the setting under the cursor does, what its current value means, and prints a **Caution** for every setting that makes a run riskier (`sync_to_peer = upgrade`, `ha_config_unsynced = caution`, `allow_downgrade`, wide session bands, `max_parallel` above 1, and so on).
- A value that is not allowed (text in a number, a band above 1, a cross-feature downgrade without `allow_downgrade`) turns red and blocks saving until it is fixed.
- The buttons work as in `select`: **Save**, **Save & Exit** (Ctrl-D), **Quit without saving** (Esc, which asks first if there are unsaved changes).

Saving writes `settings.toml` in the folder you run it from, containing only the settings that differ from the built-in defaults, so a later, safer default still reaches you. If the file already exists it is rewritten and its comments are not kept. **Every command run from that folder then uses it** and says so (`Using settings file ... (found in this folder)`); name another file with `--settings FILE`, which `settings` edits too. `prepare` pins the file it used into the upgrade script.

**Two settings to know about.**

- `sync_to_peer` is `Disable`, `Both` or `UpgradeOnly`. The default, **Both**, forces the passive member to match the active one when preparing for the upgrade cycle (`prepare`) **and again** during the upgrade itself, just before the passive member is touched, for each pair that still needs it. `UpgradeOnly` leaves `prepare` out of it. `Disable` never forces a sync; an unsynced pair is then refused or flagged according to `ha_config_unsynced`. Because the default syncs during the upgrade, a pair that is out of sync shows as needing acknowledgement (`force_config_sync`) rather than being refused.
- `panorama_compare` is `minor` (the default) or `major`, and sets the unit for `spread_limit` (default 2) and for "Panorama is older than the target". A version reads major.minor.maintenance.hotfix, so `12.1.10-h2` is major 12, minor 1, maintenance 10, hotfix 2. With `minor`, Panorama should be at most 2 feature releases ahead of a firewall: 12.2, 12.1 and 11.2 are within 2 of each other. Advice only, never a blocker.

PAN-OS numbering has gaps (10.2 was the first release to end in .2, and there is no 12.0), so minor distances are counted along the list of releases that exist, not worked out from the numbers. That list ships with the tool, taken from the vendor's end-of-life summary page, and is shown by `panupgrade releases`. It goes out of date as releases appear: `panupgrade releases --update` downloads a fresh copy into `releases.json` in the folder you run it from (a plain JSON file you may also edit by hand), and every command run from that folder then uses it and says so. `--releases FILE` names another. A release the table does not list is counted in numeric order and the advice says so. Nothing is downloaded unless you run `--update`.

Without a terminal, or with `--set`, `settings` prints the current values instead (`--no-interactive` forces this). `--set name=value` changes one run without touching any file. `settings.example.toml` lists every setting with its default for editing by hand.

## What to confirm on your first live run

These were observed in one lab only, so watch them on yours: the reply shape of the download and install jobs, how a rebooting firewall looks through Panorama, and whether `sync-to-remote` returns before or after the sync finishes (the tool polls rather than assuming). Anything it cannot read stops the step and puts the device's own words in the journal.
