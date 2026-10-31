# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""The tests never touch the real sign-in folder.

Profiles, remembered choices and trusted certificates live under ``~/.panupgrade``. A test that runs a command could find the
developer's own profile there and write into its workspace, so before anything else runs the whole test process is pointed at a
throwaway folder and cleared of every PANUPGRADE_* variable. Individual tests that need their own folder still set their own.
"""

import atexit
import os
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="panupgrade-test-home-")
for _name in [n for n in os.environ if n.startswith("PANUPGRADE_")]:
    del os.environ[_name]
os.environ["PANUPGRADE_HOME"] = _HOME
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
