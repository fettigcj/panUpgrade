# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Download the vendor's PAN-OS release list. The only place the release table touches the network, and only when asked."""

import datetime
import ssl
import urllib.error
import urllib.request

from panupgrade import releases


def fetch_page(url=releases.URL, timeout=30):
    """The page's HTML. The certificate is always checked; nothing is sent but an ordinary request."""
    request = urllib.request.Request(url, headers={"User-Agent": "panupgrade (release table update)"})
    try:
        with urllib.request.urlopen(request, context=ssl.create_default_context(), timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise releases.ReleaseError(f"{url} answered HTTP {exc.code}") from None
    except OSError as exc:
        raise releases.ReleaseError(f"cannot download {url}: {exc}") from None


def download(today=None, fetch=fetch_page):
    """A fresh table from the vendor's page."""
    return releases.Releases(releases.parse_eol_page(fetch()), releases.URL, (today or datetime.date.today()).isoformat())
