# Security

panUpgrade holds Panorama credentials and can reboot firewalls, so please report problems privately.

- Report vulnerabilities through GitHub's private vulnerability reporting for this repository ("Security" tab), not a public issue.
- Never include credentials, API keys, configurations or logs from a real environment in a report.

Design points worth knowing: credentials are read from a file you name and are never printed, logged or written to journals; Panorama's TLS certificate is verified unless you explicitly opt out; nothing changes without an approval bound to a specific plan.
