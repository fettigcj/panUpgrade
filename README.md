# panUpgrade

Upgrade a fleet of Palo Alto Networks firewalls through Panorama, safely.

panUpgrade reads your fleet, tells you what would happen to each firewall and why, and changes nothing until a person approves a specific list of firewalls. It handles HA pairs as one unit (passive member first), checks sessions before and after, keeps a journal of every step so an interrupted run can be resumed, and refuses to run on a plan that has changed since it was approved.

> **Status: pre-release (0.1.0.dev0).** It has been exercised against a lab fleet only. Read [docs/running.md](docs/running.md) before pointing it at production, and try everything with `--dry-run` first.

## The one-screen way

At a terminal, just type:

```
panupgrade
```

One screen walks through the whole job, in order: **sign in**, **choose firewalls**, **read the fleet**, **plan**, then (from the command line for now) **prepare** and **upgrade**. Each step shows where it stands. Alt-H goes home, Alt-A signs in, Alt-F chooses firewalls, Alt-R reads the fleet, Alt-P plans, Alt-S opens the settings, Alt-J lists the background jobs, and Alt-1 to Alt-9 jump to the steps in order. Reading Panorama happens in the background while the screen stays live; the last answer is kept when you leave a screen, and a wide window shows the running and finished jobs down the right-hand side. Everything the screen does is also available as a command, listed below, for scripts and for anyone who prefers them.

## How it works

| Step | Command | Changes anything? |
|---|---|---|
| See the state of the fleet | `panupgrade report` | No |
| Choose firewalls | `panupgrade select` | No |
| Change the settings | `panupgrade settings` | Only the settings file |
| See what an upgrade would do | `panupgrade plan --target X` | No |
| Get ready (content, config sync, image downloads) | `panupgrade prepare` | Yes, after you type APPROVE |
| Upgrade | the script `prepare` writes, or `panupgrade upgrade` | Yes, only the firewalls the approval names |

`prepare` finishes by writing an **upgrade kit**: a sign-off pack (HTML, text, Excel), the firewall list, the approval, and a ready-to-run script. Take the pack to whoever must agree; running the script is the go-ahead.

## Safety model

- Nothing changes without an approval that names firewalls (never "all") and acknowledges every caution on them.
- An approval is bound to the plan it was made for. If a firewall's version, HA role or planned steps change, the run is refused and the message says what changed.
- Every step is journaled before and after it runs. A step that was started but not finished is reported for you to check and is never silently repeated.
- Config is backed up and verified before any install or reboot.
- Safe defaults: an HA pair whose passive member is out of sync is blocked, and `prepare` is the only place a config sync is forced. Loosening a safety setting prints a caution.
- Panorama's TLS certificate is verified. Opt out (`--insecure-tls`) only for a lab with a self-signed certificate; it prints a warning.

## Install

Python 3.11 or newer. The core has no third-party dependencies (`openpyxl` is optional, for Excel output). A virtual environment gives you a plain `panupgrade` command and keeps its packages away from everything else on the machine.

### Build the virtual environment (once)

Create it in your working folder, not inside the checkout, so the checkout stays clean.

```
git clone <this repository>
cd <your working folder>
python -m venv .venv
```

Activate it. Your prompt then starts with `(.venv)`.

```
# Windows PowerShell
.venv\Scripts\Activate.ps1

# Linux or macOS
source .venv/bin/activate
```

Install panUpgrade into it. `-e` makes it editable: a `git pull` in the checkout takes effect at once, with no reinstall. The first install downloads the `setuptools` build tool, so it needs internet.

```
python -m pip install -e <path to the panUpgrade checkout>
```

### Use the virtual environment (every new terminal)

Go to your working folder and activate the environment. Nothing else is needed; the `panupgrade` command now works from anywhere.

```
cd <your working folder>
.venv\Scripts\Activate.ps1        # Linux or macOS: source .venv/bin/activate
panupgrade help
```

Leave the environment with `deactivate`. If `panupgrade` is "not found", the environment isn't active: check for `(.venv)` at the start of the prompt.

More detail, including running the tests, is in [docs/venv.md](docs/venv.md).

## First look (no firewall needed)

```
panupgrade plan --source fake:fixtures/sample-fleet.json --target 20.2.9
```

The sample fleet is invented. Add `--verbose` to list every check, and `--out DIR` to write `plan.json`, `plan.txt` and `plan.html`.

## Your own Panorama

Keep your settings, firewall lists, run journals and kits in a working folder of your own, outside the checkout.

**You decide how panUpgrade signs in.** It assumes nothing about how your Panorama accounts are arranged:

| Your setup | What to do |
|---|---|
| One account for everything | Put it in the credentials file. If the file holds only that account, nothing else is needed. |
| A read-only account for the tool, and an administrator's own login for changes | Put the tool's account in the file and add `--ask-credentials changes`. Reading uses the file; the steps that change firewalls ask for a username and password. |
| An administrator's own login for everything | `--ask-credentials all`. Nothing is read from a file. |
| Two accounts, both in the file | `--account NAME` for reading and `--work-account NAME` for changes. |
| Several Panoramas | Make a **profile** for each with `panupgrade auth`: its address, its own admin accounts, and its own firewall list, settings and records, kept apart. `--profile NAME` picks one; naming a Panorama's address picks its profile. |

A typed login is used for that run only; it is never stored, printed or logged. The credentials file is a dict of account name to either a password or a ready-made API key, for example `{'person': 'their-password', 'service': {'api_key': 'KEY'}}`. An account kept as an API key signs in with that key and needs no login. Keep the file in your own home folder and out of version control. Every command tells you which Panorama, credentials file and account it is using before it connects.

| Setting | Flag | Environment variable | Default |
|---|---|---|---|
| Panorama profile | `--profile` | `PANUPGRADE_PROFILE` | the one matching `--panorama`, else the one used last |
| Credentials file | `--creds-file` | `PANUPGRADE_CREDS` | the profile's `credentials.txt` |
| Account to sign in with | `--account` | `PANUPGRADE_ACCOUNT` | the profile's choice, else the only account in the file |
| A different account for changes | `--work-account` | `PANUPGRADE_WORK_ACCOUNT` | the profile's choice, else the same as `--account` |
| Skip certificate checks (lab only) | `--insecure-tls` | `PANUPGRADE_INSECURE_TLS=1` | off |

Then follow [docs/running.md](docs/running.md). Every setting and its default is listed in [settings.example.toml](settings.example.toml).

## More

- [docs/running.md](docs/running.md): the day-to-day workflow
- [docs/execution.md](docs/execution.md): what the executor guarantees
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md)

## Tests

```
python -m unittest discover -s tests -t .
```

Standard library only. Fixtures are invented; a test fails if they contain real-looking addresses or versions, and another fails if site-specific names, addresses or paths creep into the repository.

## Credits

Ideas (dry run, pre/post snapshots, readiness checks, parallelism, direct and Panorama-proxied modes) are acknowledged from cdot65's `pan-os-upgrade` and `pan-os-upgrade-web`. This code is written clean-room; none of it is copied from those projects.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE). Run it against anything you like; if you distribute a modified version, share its source under the same terms. As the license says, it comes with no warranty: test before you trust it with production firewalls.
