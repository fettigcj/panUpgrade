# panUpgrade

Upgrade a fleet of Palo Alto Networks firewalls through Panorama, safely.

panUpgrade reads your fleet, tells you what would happen to each firewall and why, and changes nothing until a person approves a specific list of firewalls. It handles HA pairs as one unit (passive member first), checks sessions before and after, keeps a journal of every step so an interrupted run can be resumed, and refuses to run on a plan that has changed since it was approved.

> **Status: pre-release (0.1.0.dev0).** It has been exercised against a lab fleet only. Read [docs/running.md](docs/running.md) before pointing it at production, and try everything with `--dry-run` first.

## How it works

| Step | Command | Changes anything? |
|---|---|---|
| Choose firewalls | `panupgrade select` | No |
| See the plan | `panupgrade plan` | No |
| Get ready (content, config sync, image downloads) | `panupgrade prepare` | Yes, after you type APPROVE |
| Upgrade | the script `prepare` writes, or `panupgrade upgrade` | Yes, only the firewalls the approval names |

`prepare` finishes by writing an **upgrade kit**: a sign-off report (HTML, text, Excel), the firewall list, the approval, and a ready-to-run script. Take the report to whoever must agree; running the script is the go-ahead.

## Safety model

- Nothing changes without an approval that names firewalls (never "all") and acknowledges every caution on them.
- An approval is bound to the plan it was made for. If a firewall's version, HA role or planned steps change, the run is refused and the message says what changed.
- Every step is journaled before and after it runs. A step that was started but not finished is reported for you to check and is never silently repeated.
- Config is backed up and verified before any install or reboot.
- Safe defaults: an HA pair whose passive member is out of sync is blocked, and `prepare` is the only place a config sync is forced. Loosening a safety setting prints a caution.
- Panorama's TLS certificate is verified. Opt out (`--insecure-tls`) only for a lab with a self-signed certificate; it prints a warning.

## Install

Python 3.11 or newer. The core has no third-party dependencies (`openpyxl` is optional, for Excel output).

```
git clone <this repository>
cd panUpgrade
python -m venv .venv
pip install -e .
```

See [docs/venv.md](docs/venv.md) for step-by-step virtual-environment help.

## First look (no firewall needed)

```
panupgrade plan --source fake:fixtures/sample-fleet.json --target 20.2.9
```

The sample fleet is invented. Add `--verbose` to list every check, and `--out DIR` to write `plan.json`, `plan.txt` and `plan.html`.

## Your own Panorama

Keep your settings, firewall lists, run journals and kits in a working folder of your own, outside the checkout. Tell panUpgrade where your credentials are once:

| Environment variable | Meaning | Default |
|---|---|---|
| `PANUPGRADE_CREDS` | file holding a dict of account to password | `~/.panupgrade/credentials.txt` |
| `PANUPGRADE_READ_ACCOUNT` | read-only Panorama account | `panupgrade-read` |
| `PANUPGRADE_WORK_ACCOUNT` | account that makes changes in a real run | `panupgrade-write` |
| `PANUPGRADE_INSECURE_TLS` | `1` to skip certificate checks (lab only) | off |

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
