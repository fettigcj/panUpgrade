# Running panUpgrade in a virtual environment

A virtual environment ("venv") is a private folder of Python packages for one tool. Use one so panUpgrade's dependencies never clash with anything else on the machine. Phase 1 has no third-party dependencies, but later phases will.

Requirements: a current Python (3.11 or newer; developed on 3.14). Newer is better; there is no legacy support.

Each step is one command.

1. Go to the repository folder.

```powershell
cd <path to your panUpgrade checkout>
```

2. Create the environment in a folder called `.venv`.

```powershell
python -m venv .venv
```

3. Switch to it. Your prompt shows `(.venv)` once it is active.

```powershell
.venv\Scripts\Activate.ps1
```

4. Install panUpgrade into the environment (editable, so edits to the code take effect at once). This downloads the build tool `setuptools`.

```powershell
python -m pip install -e .
```

5. Run the tests. They need no network and no firewall.

```powershell
python -m unittest discover -s tests -t .
```

6. Try a plan against the invented demo fleet. This only reads the file and prints a report.

```powershell
panupgrade plan --source fake:fixtures/sample-fleet.json --target 20.2.9 --accept-standalone-outage
```

7. When you are finished, leave the environment.

```powershell
deactivate
```

Linux and macOS: step 3 is `source .venv/bin/activate`; the other steps are the same.

If you do not want to install anything, you can run it straight from the source folder instead of steps 2-4:

```powershell
$env:PYTHONPATH = "src"; python -m panupgrade plan --source fake:fixtures/sample-fleet.json --target 20.2.9
```

## Later phases
When the live source arrives, dependencies will be listed in `pyproject.toml` with exact versions and file hashes, so `pip` refuses anything that does not match. Keep one environment per tool; do not share it with other tools.
