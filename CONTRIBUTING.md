# Contributing

## Ground rules
- **Safety first.** Anything that changes a firewall needs an explicit approval path and a test. The `panupgrade` package must never open a network connection; that is confined to `panupgrade_live` (a test enforces it).
- **No site-specific content.** No real hostnames, addresses, serials, credentials, account names or local paths in code, docs, tests or fixtures. `tests/test_portable.py` fails if they appear. Keep your own settings and data in a working folder outside the checkout.
- **Evidence over assertion.** Say what a behaviour was verified against. If it is not verified, say "unverified" in the code or docs.
- **Standard library only** in the core. A new dependency needs a strong reason.

## Setup
```
python -m venv .venv
pip install -e .
python -m unittest discover -s tests -t .
```

## Writing output people will actually read
Output is the product's face. Lead with what the reader must decide or fix, group by importance, use short bullets, and say each thing once. A refusal states what was wrong and what to do next. Routine detail is counted or hidden behind `--verbose`, not printed line by line.

## Pull requests
- One change per PR, with tests. Run the full suite first.
- Update `CHANGELOG.md` under "Unreleased".
- Describe what was run and what it showed. Do not paste credentials, configs or logs that contain them.
