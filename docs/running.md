# Running panUpgrade

## The one-screen way

`panupgrade` with no arguments, typed at a terminal, opens a single screen for the whole workflow. The top has three rows: the **Panorama profile** (Left and Right switch between your Panoramas; Enter manages them), its **address**, and the **target PAN-OS version**. The address and target are kept in the profile, so each Panorama remembers its own; with no profile they are remembered as typed (`~/.panupgrade/state.json`, which holds no secrets). Below them are the steps, each with a line saying where it stands:

Opening a step never starts anything. The fleet report, the plan, Prepare and Upgrade each open with a **Start** button (Prepare and Upgrade: "Start: read the plan"), and nothing talks to Panorama until you press it. While a read is going, the first button is **Abort** (it stops before its next call to Panorama; a call already in progress has to return first) and there is no Refresh; once there is an answer it becomes **Refresh**. A real Prepare or Upgrade run is never torn down mid-step: its first button is **Stop after the steps in progress**. The one exception is the list of PAN-OS releases, which reads a local file and opens ready.

1. **Sign in**: the `auth` screen. If a step finds it cannot sign in, the screen takes you here and brings you back to the step when you save.
2. **Choose firewalls**: the `select` form, saving `upgradeList.txt` in the profile's workspace.
3. **Fleet report**: what `panupgrade report` prints, read in the background. The last answer is kept, with how old it is, however many times you leave the screen and come back (and it is waiting if it finished while you were elsewhere). R refreshes it, and the old answer stays on show until the new one arrives; Up, Down, PgUp and PgDn scroll.
4. **Plan the upgrade**: what `panupgrade plan --target ...` prints. Read-only.
5. **Prepare**: the `prepare` command in three steps on one screen. It first reads the fleet and shows the plan (what it would sync, install and download; nothing is changed). Press "Approve and run..." and the screen lists exactly what you are approving; nothing starts until you type APPROVE (any case), the same word the command asks for. The approval is bound to the plan that was read, so a plan older than 30 minutes must be read again first, and a different address or target discards it. The run then shows its progress live; leaving the screen does not stop it (Alt-J lists it), and quitting while it is changing firewalls asks you to press the key a second time. "Rehearse (dry run)" runs the same steps against a model of the fleet and sends nothing to any firewall. When it finishes, the report and plan you read earlier are forgotten, because the fleet has changed; the upgrade kit is written as the command writes it.
6. **Upgrade**: the `upgrade` command in the same three steps as Prepare. It reads the fleet and shows the plan exactly as `plan` prints it (nothing is changed). "Approve and run..." then lists every firewall that will be upgraded with its role and versions, the order of the batches, how many outage steps and HA failovers there are, and every caution that approving acknowledges; nothing starts until you type APPROVE. The approval is bound to the plan that was read (a plan older than 30 minutes, or for a different address or target, is refused) and is written next to the run's journal. If an earlier run of this exact plan did not finish, you choose between "Approve and CONTINUE the earlier run" (finished steps are skipped, an in-doubt step is reported, never repeated) and "Approve and start a NEW run"; the screen never asks the keyboard a question of its own. Standalone firewalls are left out unless you switch them in (each is offline while it reboots). While it runs, "Stop after the steps in progress" lets the steps already going finish and starts nothing new; the run can be continued later. Leaving the screen does not stop it, and quitting while it changes firewalls asks you to press the key a second time. "Rehearse (dry run)" runs it against a model of the fleet and sends nothing to any firewall.

   While an upgrade runs, the screen shows it **unit by unit**: a list of units on the left (an HA pair is one unit, named passive first, "fw-two + fw-one"; a standalone firewall is its own), each marked waiting, running (with the step it is on), done or failed, and on the right the time-stamped log of the chosen unit alone, every line naming the firewall it belongs to. The right-hand log follows the unit that is running until you choose another with Up and Down; PgUp, PgDn, Home and End scroll it. V (or the "View" button) switches to the plain combined output. After the run ends the logs can still be read; a run that ended without doing anything (refused, or failed to start) shows its plain output, where the reason is. This view takes the place of the jobs pane unless the window is very wide.
7. **Settings**: the `settings` form.
8. **PAN-OS releases**: the table used to count minor versions.
9. **Jobs**: every piece of background work (reading the fleet, reading the firewall list, a prepare), newest first. Each job keeps a time-stamped log of what it was doing ("asking Panorama: show devices all", one line per call) followed by everything it printed. Up and Down choose a job; Tab (or Right) moves into its log, where Up, Down, PgUp, PgDn, Home and End scroll the whole history (a running job's log follows its newest line until you scroll up); Tab (or Left) goes back to the list. In a window at least 120 columns wide the last five lines of the newest job, and of every job still running, run down the right-hand side of every screen except the forms (the sign-in and settings screens need their room until the window is about 170 wide); in a narrower one the footer says how many jobs are running.

Alt-H (home), Alt-A (sign in), Alt-F (firewalls), Alt-R (report), Alt-P (plan), Alt-S (settings), Alt-J (jobs) and Alt-1 to Alt-9 (the steps in order) work from every screen. A screen with unsaved changes asks you to press the key a second time before it lets them go. Ctrl-C leaves from anywhere. On Linux and macOS terminals, Alt arrives as Esc plus the letter (on macOS Terminal, turn on "Use Option as Meta key").

`panupgrade shell --panorama ADDRESS --target VERSION` starts it with those filled in. The commands below do the same things one at a time and are what scripts should use.

The whole workflow on one page. Commands are PowerShell; on Linux or macOS use the same arguments in your shell.

## Before you start

Work from a folder of your own, not the checkout. Runs, backups and the upgrade kit are written under the current folder.

```powershell
pip install -e <path to your panUpgrade checkout>
cd <your working folder>
```

**Panorama profiles: one workspace per Panorama.** A site with several Panoramas keeps them apart. A profile is a folder, `~/.panupgrade/profiles/<name>/`, holding everything that belongs to one Panorama and nothing that belongs to another:

- its **address** and which **admin accounts** it has (`credentials.txt`), with the choice of which account reads and which makes changes;
- the **firewall list** (`upgradeList.txt`), **settings** (`settings.toml`), **release table** (`releases.json`) and the **target version** last used;
- the **records** of what was done there: run journals (`runs/`), upgrade kits (`prepared/`) and configuration backups (`backups/`).

`panupgrade auth` is where profiles and the admin accounts under them are made:

```powershell
panupgrade auth
```

Switch profile with Left and Right, make one with **New profile** (a name and an address), and under each profile **add** admin accounts (type the name and a hidden password or API key, or type the password once and keep only the API key it gets back), **remove** them, choose which account **reads** and which makes **changes**, and **test** a sign-in. The same user name can have different passwords on different Panoramas because each profile has its own accounts. Nothing reaches the disk until you Save. **Remove profile** does not delete anything: the folder is renamed `<name>.removed-<date>`, because it may hold configuration backups and run journals.

Which profile a command uses: `--profile NAME`; else, when `--panorama` names an address, the profile with that address; else the `PANUPGRADE_PROFILE` environment variable; else the one used last (or the only one there is). A Panorama named on the command line that matches no profile is simply used as it is, with the current folder as its workspace and a credentials file named by `--creds-file`. The upgrade script `prepare` writes carries `--profile`, so it runs in the same workspace.

When a command does not name a Panorama or a profile, it uses the profile you worked in last; or, on Windows, you can choose "always the profile above" on the sign-in screen, which sets the user-level variable `PANUPGRADE_PROFILE`. The accounts table shows, for each account, when its sign-in was last tested and whether it worked ("connected successfully on ..." or "failed on ...", with the reason underneath); the results are kept in `sign_ins.json` in the profile's folder, never with the secrets. The environment variables `PANUPGRADE_CREDS`, `PANUPGRADE_ACCOUNT` and `PANUPGRADE_WORK_ACCOUNT` still override what a profile says; the sign-in screen warns when one is set in your terminal. Any command run at a terminal that finds no credentials, or no account to use, opens the sign-in screen by itself and carries on in the profile you make or fix; without a terminal it stops with an error that says what is missing, and `panupgrade auth` prints a summary of the profiles.

**A Panorama with a self-signed certificate.** Most Panoramas present a certificate their own CA made, which a normal check rejects. Anything that connects (adding an account with "sign in now", Test sign-in, a report, a plan, choosing firewalls) then stops with the reason, the certificate's SHA-256 fingerprint, and **Accept risk & continue?** On a terminal outside the screen it is a plain `[y/N]` question; in a script it is just the error. Compare the fingerprint with the one Panorama shows for its own certificate (Device > Certificate Management > Certificates) before accepting. Accepting does not switch checking off: it records exactly that certificate for that address (`~/.panupgrade/trusted_certs.json`, public fingerprints only), and from then on a connection must present the same certificate. If it ever differs, the connection is refused with a message saying so, because it means a replaced certificate or someone in the middle. **Forget certificate** on the sign-in screen (with the address filled in) takes the acceptance back. `--insecure-tls` still exists for a throwaway lab, but this is the way to do it properly.

The pieces a profile manages, if you would rather do it by hand:

- **A credentials file** holds a dict of account name to either a password, or a ready-made API key written `{'api_key': 'KEY'}`, for example `{'person': 'their-password', 'service': {'api_key': 'KEY'}}`. A password account is logged in with each run; an API key account is used as it is. It is the profile's `credentials.txt`, or the file named by `--creds-file` or `PANUPGRADE_CREDS`. If it holds one account, that account is used. If it holds several, name one with `--account` (or `PANUPGRADE_ACCOUNT`).
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

- **Up / Down / Tab** move between settings; Down past the last one reaches the buttons, and Down again wraps to the top (Up does the same in reverse). **Left / Right** step through a choice (`sync_to_peer`: never, prepare, upgrade) or flip a yes/no. Numbers and text are typed.
- **Ctrl-R** (or Delete) puts a setting back to its default.
- **PgUp / PgDn** scroll the explanation when it is taller than the window; its last line says so and shows which lines you are looking at. The buttons always stay on screen.
- The explanation of the setting under the cursor sits to the right of the list (below it in a window narrower than about 110 columns): what it does, then every choice it has with what each means (the current one marked with `>`), and a **Caution** for every setting that makes a run riskier (`sync_to_peer = upgrade`, `ha_config_unsynced = caution`, `allow_downgrade`, wide session bands, `max_parallel` above 1, and so on).
- A value that is not allowed (text in a number, a band above 1, a cross-feature downgrade without `allow_downgrade`) turns red and blocks saving until it is fixed.
- The buttons work as in `select`: **Save**, **Save & Exit** (Ctrl-D), **Quit without saving** (Esc, which asks first if there are unsaved changes).

Saving writes `settings.toml` containing only the settings that differ from the built-in defaults, so a later, safer default still reaches you. If the file already exists it is rewritten and its comments are not kept. In a profile it is saved in the profile's workspace; with no profile, in the folder you run it from. **Every command in that workspace then uses it** and says so (`Using settings file ... (found in the profile lab)`); name another file with `--settings FILE`, which `settings` edits too. `prepare` pins the file it used into the upgrade script.

**Two settings to know about.**

- `sync_to_peer` is `Disable`, `Both` or `UpgradeOnly`. The default, **Both**, forces the passive member to match the active one when preparing for the upgrade cycle (`prepare`) **and again** during the upgrade itself, just before the passive member is touched, for each pair that still needs it. `UpgradeOnly` leaves `prepare` out of it. `Disable` never forces a sync; an unsynced pair is then refused or flagged according to `ha_config_unsynced`. Because the default syncs during the upgrade, a pair that is out of sync shows as needing acknowledgement (`force_config_sync`) rather than being refused.
- `panorama_compare` is `minor` (the default) or `major`, and sets the unit for `spread_limit` (default 2) and for "Panorama is older than the target". A version reads major.minor.maintenance.hotfix, so `12.1.10-h2` is major 12, minor 1, maintenance 10, hotfix 2. With `minor`, Panorama should be at most 2 feature releases ahead of a firewall: 12.2, 12.1 and 11.2 are within 2 of each other. Advice only, never a blocker.

PAN-OS numbering has gaps (10.2 was the first release to end in .2, and there is no 12.0), so minor distances are counted along the list of releases that exist, not worked out from the numbers. That list ships with the tool, taken from the vendor's end-of-life summary page, and is shown by `panupgrade releases`. It goes out of date as releases appear: `panupgrade releases --update` downloads a fresh copy into `releases.json` in the folder you run it from (a plain JSON file you may also edit by hand), and every command run from that folder then uses it and says so. `--releases FILE` names another. A release the table does not list is counted in numeric order and the advice says so. Nothing is downloaded unless you run `--update`.

Without a terminal, or with `--set`, `settings` prints the current values instead (`--no-interactive` forces this). `--set name=value` changes one run without touching any file. `settings.example.toml` lists every setting with its default for editing by hand.

## What to confirm on your first live run

These were observed in one lab only, so watch them on yours: the reply shape of the download and install jobs, how a rebooting firewall looks through Panorama, and whether `sync-to-remote` returns before or after the sync finishes (the tool polls rather than assuming). Anything it cannot read stops the step and puts the device's own words in the journal.
