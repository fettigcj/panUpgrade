# How an upgrade is executed

`panupgrade upgrade` carries out an approved plan: against the **simulator** for a fake source, against a model of the live fleet with `--dry-run`, and against real firewalls (through the allow-listed live backend in `panupgrade_live`) for a live source. No code in the `panupgrade` package itself can reach a firewall (a test lists the `Backend` subclasses and fails if there is any other).

## The flow
1. `panupgrade plan ... --out out/` writes the plan and `approval.template.json` (plan hash, empty `serials`, list of `cautions_available`).
2. The operator edits the template: names the devices (never "all"), copies each caution they accept into `acks` (`"<serial|fleet>:<code>"`), and sets `approved_by`.
3. `panupgrade upgrade ... --approval approval.json --journal run.jsonl` re-plans from the source and refuses (exit 3) unless the plan hash still matches, every approved device is executable, and every fleet caution and every caution on an approved device is acknowledged. Because it re-plans, a fleet that changed since approval is refused rather than run.
4. `--resume` continues the same journal. `--sim-fail serial:operation` injects a failure for rehearsal.

## Rules the executor enforces (each has a test)
- Every step is journaled `started` before and `ok`/`failed` after, fsynced. A step left `started` is **in doubt**: resume reports `needs_attention` and does not replay it.
- No exception from a device operation is swallowed; it fails the step and the device and counts toward `max_failures` (default 5). After that, no new device starts.
- Install and reboot refuse to run unless that device's own backup step is `ok` in the journal. A backup that is not parsed and checksummed is a failure.
- Pre-flight at execution time: the device must still report the version the plan assumed, have no pending config (unless the setting allows it), and no running jobs.
- A device is judged on the version it reports after *its own* reboot (reading it too early would report the previous device's).
- An HA pair is one unit, passive first; if the first member fails the second is `held`. A member that is no longer active when its turn comes skips the failover step.
- Failover: the new active node's sessions must be within `failover_band` of the old one's, else the suspension is reverted and the device fails. After the upgrade, the active node must be within `post_upgrade_band` of the pair's baseline (taken once, from the node that was active before the unit started).
- Members suspended by the tool are released with `go live`, then `verify HA` (HA1 config sync, HA2 session sync) is judged.
- A failed device is never retried automatically on resume.

## Offline device queries (`xmlops.py`)
`op(command) -> <response>` is the one seam to a device. Parsers turn responses into the same `Facts` the planner uses; a missing or unparseable piece is `None` ("no data"), never a guess. `CannedOp` scripts responses per command and fails loudly on any command nobody scripted. A live adapter will wrap pan-os-python's `Firewall.op` behind the same callable.

## UNVERIFIED (assumed, to be confirmed against a real firewall)
- Response shapes in the canned XML come from Upgrade Assurance's parsers, not from a recording. In particular the HA2 element `state-sync` and the `preemptive` field are guesses.
- Whether a suspended state survives a reboot: one observation says it does not (a lab member suspended by the tool whose reboot step timed out came back as passive, without the tool ever releasing it; no preemption, PAN-OS 10.2.3). `go live` still releases it explicitly, which is harmless if there is nothing to release. Not known for a pair with preemption on. What the simulator does for roles after `go live` with preemption (it hands the active role back to the original active).
- Exact commands for suspend/functional, restoring roles, and rollback; a rollback is not automated.
- Timing, job progress and timeouts are not modelled.
- Units run in parallel, up to `max_parallel` (default 5) at a time: each HA pair (or standalone firewall) is one job whose members go in series, passive first, and the next unit starts as soon as one finishes. The simulator does not model timing, so it shows the interleaving but not how long a reboot takes.
