# Running panupgrade (PowerShell, from the project folder)

```powershell
# once: point at the checkout (or `pip install -e <checkout>` and skip this line)
$env:PYTHONPATH = "<path to your panUpgrade checkout>\src"

# then work from a folder of your own, not the checkout: runs, backups and the upgrade kit are written under the current folder
# (override with --runs-dir, --kit-dir and the backup_dir setting)
cd <your working folder>
```

Credentials: a file holding a dict of account to password (default `~/.panupgrade/credentials.txt`; set `PANUPGRADE_CREDS` or pass
`--creds-file`). Reads use a read-only account (`--account`, or `PANUPGRADE_READ_ACCOUNT`; default `panupgrade-read`); a real run
changes things as a second account (`--work-account`, or `PANUPGRADE_WORK_ACCOUNT`; default `panupgrade-write`). Nothing prints a password.

Panorama's TLS certificate is verified. A lab Panorama with a self-signed certificate needs `--insecure-tls` (or `PANUPGRADE_INSECURE_TLS=1`); a warning is printed whenever checking is off.

## The loop

1. **Pick the firewalls** (changes nothing). Criteria combine; the output is a list you check and edit:
   ```powershell
   python -m panupgrade select --source panorama:<panorama-address> --version-is 10.2.1 --name-contains <text> --out firewalls.txt
   ```
   Criteria: `--name-contains`, `--name` (glob), `--version-is` (glob), `--version-older-than`, `--version-newer-than`, `--ha-state`,
   `--model`, `--device-group`, `--tag`, `--serial`. Then use `--serials-file FILE` everywhere below.
2. **Plan** (changes nothing): what would happen to each firewall, why some are blocked, and what needs acknowledging.
   ```powershell
   python -m panupgrade plan --source panorama:<panorama-address> --serials-file firewalls.txt --target 10.2.2 --settings settings.toml --xlsx readiness.xlsx
   ```
3. **Prepare** (optional, but it makes the window shorter and calmer). Ahead of the window it:
   - brings dynamic content (apps/threats, antivirus, WildFire) level between HA members first (`sync_content = "prepare"`), and a mismatch that cannot be levelled GATES the config sync below: it is not even attempted;
   - forces active -> passive config sync on pairs that read "not synchronized" (needs `sync_to_peer = prepare`; backs up the passive first);
   - with `--target V`, pre-downloads the image (and any base image the path needs) onto every selected firewall that lacks it.
   It never installs, reboots or fails anything over. Whatever it cannot fix (low disk, pending changes, unhealthy HA) stays for `plan` to report;
   after a real run it re-plans for the target and lists what is still blocked.
   Dry run first, then the real thing (it asks you to type APPROVE and shows exactly what you are approving):
   ```powershell
   python -m panupgrade prepare --dry-run --source panorama:<panorama-address> --serials-file firewalls.txt --target 10.2.2 --settings settings.toml
   ```
   ```powershell
   python -m panupgrade prepare --source panorama:<panorama-address> --serials-file firewalls.txt --target 10.2.2 --settings settings.toml
   ```
   When the preparation is done, `prepare` re-reads the fleet, prints the upgrade to the target as it now stands (what is blocked,
   what needs acknowledging, the cautions that running the script will acknowledge) and writes the **upgrade kit** into `prepared\`
   (change with `--kit-dir`): a **sign-off report** (HTML, text and Excel), the firewall list, the approval, and a ready-to-run
   script (`run-upgrade-<target>-<time>.bat` on Windows, `.sh` elsewhere). There is no second prompt: take the report to whoever
   must agree, and come back to the script.
4. **Upgrade.** When the window opens and the people who must agree have, run the script. **Running it is the go-ahead.** It is the `upgrade` command with the approval already in it, and it prints the cautions it proceeds on:
   ```powershell
   prepared\run-upgrade-10.2.2-20261029-130000.bat
   ```
   The approval holds only while the plan it was made for is unchanged (the firewalls, their versions and HA roles, the planned
   steps and the cautions; not free disk, session counts or timestamps) and for `approval_max_age_hours` (default 168, a week: time to take the report to the people who must agree). If an admin
   swapped HA roles in the meantime, or a new caution appeared, the upgrade refuses and says to run `prepare` again.
   A dry run of the upgrade (`upgrade --dry-run ...`) asks for nothing, because it changes nothing:
   ```powershell
   python -m panupgrade upgrade --dry-run --source panorama:<panorama-address> --serials-file firewalls.txt --target 10.2.2 --settings settings.toml
   ```
   You can also skip `prepare` and run `upgrade` directly without `--dry-run`; it then shows the plan and asks you to type APPROVE.

Every run keeps a journal of what it did, and you never name it: a new time-stamped file is created under `runs\` and its path is
printed at the start. Nothing is ever overwritten (that file is the record of what was done to the firewalls, and what makes a
half-finished run safe to continue). `prepare` is safe to repeat as it is, because it checks the firewalls themselves. If an
`upgrade` of exactly the same plan did not finish, the tool says so and asks: type **CONTINUE** to carry on with it (finished
steps are skipped, a step that was started but not finished is reported for you to check and never repeated, a failed device is
not retried automatically) or **NEW** for a fresh run. Without a terminal add `--resume` or `--fresh`. Config backups land in the
`backup_dir` setting before any install or reboot. (`--journal FILE` and `--runs-dir DIR` override the defaults.)

## Approving

There is one requirement: a person decided to go ahead knowing what will change and which cautions apply. In the kit flow the
decision is the act of running the script, after the report has been read and agreed; `prepare`'s own tasks (config sync, image
downloads) are confirmed by typing **APPROVE**, since they change firewalls straight away. Running `upgrade` directly (without a
kit) shows the plan and asks you to type APPROVE. Every approval records who and when, names exactly the firewalls listed, and
stops working if the plan underneath it changes (firewalls, versions, HA roles, planned steps, cautions) or after
`approval_max_age_hours` (a week).

## Tweaking behaviour

`settings.example.toml` lists every setting with its default. Change one run with `--set name=value`, or keep a file and pass
`--settings FILE`. Loosening settings (`sync_to_peer`, `allow_downgrade`, wide session bands, `max_parallel`) print a caution.

## What to watch on the first live run (still unverified)

The reply shape of the download/install job, how a rebooting firewall looks through Panorama, and whether `sync-to-remote`
returns before or after the sync finishes (the tool polls rather than assuming). On a new kind of environment, anything it cannot read stops the step with
the device's own words in the journal.
