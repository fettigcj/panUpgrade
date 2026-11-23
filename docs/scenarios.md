# The four kinds of firewall, and how each is handled

panupgrade treats every firewall as exactly one of four kinds. The kind decides what a reboot costs and what the tool does about it. A firewall is never two kinds at once: **a member of an HA pair is never a member of a group**, and a plan that names one in a group refuses it.

| | Kind | A reboot means | Status |
|---|---|---|---|
| 1 | Stand-alone | an outage | built |
| 2 | Load-balanced group of stand-alone firewalls | the other members carry the traffic | built, lab-tested in pieces |
| 3 | Active/Passive HA pair | a fail-over, no outage | built |
| 4 | Active/Active HA pair | asymmetric traffic is normal there | **not built yet** |

## 1. Stand-alone

One firewall, nothing behind it. Its reboot is an outage, so the plan skips it unless you accept that (`accept_standalone_outage` or `--accept-standalone-outage`). A group with only one firewall named in it is not a group: it is treated as this kind, and the plan says so.

## 2. Load-balanced group

Two or more stand-alone firewalls behind a load balancer, named in the firewall list as `SERIAL, group-name`. The group is the unit of the upgrade:

1. Pick the member or members to reboot (at most `group_install_tolerance` at a time; a member is "in progress" from its drain, or its install when there is no drain, to the end of its service check; downloads and backups are done ahead for everyone).
2. Make the whole group ready to receive their sessions: the asymmetry window (`group_tolerate_asymmetry`, on by default) sets `tcp-reject-non-syn` to no and `asymmetric-path` to bypass, committed on the firewall, on EVERY member of the group, **whether or not it is in this run**. The sessions a rebooting member drops land on the rest of its group.
3. Drain the selected members if a drain is set up (`lb_drain_profile`, `lb_drain_probe_protocol`, `lb_drain_wait_s`): the probe's protocols are taken out of the interface management profile so the load balancer sees the probe fail. The drain comes BEFORE the install, so the balancer has the whole install to move the traffic away.
4. Install and reboot the selected members, then check each one carries traffic again before the next goes.
5. Put every setting back, whether the run succeeded or not. A firewall that could not be put back is reported loudly.

A member that comes back is in service and receives NEW sessions at once; that is harmless, because a new session starts with its own handshake. Only flows drained half-way are asymmetric.

**A member with an image already installed and waiting for its reboot** (not normal in production: an administrator should have noticed) refuses commits, so the window cannot open on it. It is rebooted first, without a window, because it is about to go down and the others take its sessions. Its own window opens once it is back, and the rest of the group does not start until that is done, whatever `group_install_tolerance` is.

**Uncommitted candidate configuration** on any firewall the window touches is never committed: a commit commits everything in the candidate, and the tool cannot know whether it is legitimate. The plan blocks until you commit it yourself, or set `pending_changes = "revert"`, which tells the tool to discard it (revert to the running configuration) before the firewall is touched. That is journaled.

## 3. Active/Passive HA pair

The standard pair. One unit of two members, the passive one first by default (`upgrade_first` decides). The active member is suspended so traffic fails over to its peer, then its session count is checked; if traffic cannot be confirmed on the peer the tool puts the roles back as they were and stops. Outage avoidance is the first priority. `pair_tolerate_asymmetry` (off by default) opens the same window on the two members.

## 4. Active/Active HA pair

Built for networks where asymmetric traffic cannot be avoided. **Not built yet.** The tool has no handling written for an Active/Active pair's traffic checks, so do not rely on it to upgrade one until this section is written.
