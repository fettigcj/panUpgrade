# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""A small .xlsx writer (standard library only) for the readiness workbook.

Sheet 'Firewalls' keeps the columns of the legacy upgradeFirewalls.xlsx so reports line up side by side:
serial, hostname, haState, upgradeable, details, startingVersion, endingVersion. Sheet 'Checks' adds one row per check.
"""

import zipfile
from xml.sax.saxutils import escape

LEGACY_HEADERS = ["serial", "hostname", "haState", "upgradeable", "details", "startingVersion", "endingVersion"]
_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def legacy_rows(plan):
    """One row per device in the legacy workbook's vocabulary; this is a report, so ending == starting."""
    rows = []
    for dev in plan["devices"]:
        reason = ""
        for status in ("fail", "error", "skip", "warn"):
            hit = next((r for r in dev["results"] if r["status"] == status and r["check"] != "selection"), None)
            if hit:
                reason = f"{hit['check']}: {hit['reason']}"
                break
        rows.append([dev["serial"], dev["hostname"], dev.get("ha_state") or "disabled",
                     "True" if dev["verdict"] in ("ready", "caution") else "False",
                     f"{dev['verdict']}: {reason}" if reason else dev["verdict"],
                     dev["current_version"], dev["current_version"]])
    return rows


def check_rows(plan):
    header = ["serial", "hostname", "check", "status", "kind", "reason", "acknowledge"]
    return header, [[d["serial"], d["hostname"], r["check"], r["status"], r.get("kind", ""), r["reason"], r.get("ack", "")]
                    for d in plan["devices"] for r in d["results"]]


def _sheet(header, rows):
    def cell(ref, value, style=0):
        return f'<c r="{ref}" t="inlineStr" s="{style}"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'
    out = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{_MAIN}"><sheetData>']
    out.append('<row r="1">' + "".join(cell(f"{chr(65 + i)}1", h, 1) for i, h in enumerate(header)) + "</row>")
    for n, row in enumerate(rows, start=2):
        out.append(f'<row r="{n}">' + "".join(cell(f"{chr(65 + i)}{n}", v) for i, v in enumerate(row)) + "</row>")
    out.append("</sheetData></worksheet>")
    return "".join(out)


def write_workbook(path, plan):
    write_sheets(path, [("Firewalls", LEGACY_HEADERS, legacy_rows(plan)), ("Checks", *check_rows(plan))])


def write_runlog(path, data):
    """The workbook of a run's log (``runlog.build`` data): the result per firewall, and every event in order."""
    write_sheets(path, [
        ("Result", ["hostname", "serial", "state", "detail"], [[o["host"], o["serial"], o["state"], o["detail"]] for o in data["outcome"]]),
        ("All activity", ["time", "hostname", "kind", "event"], [list(row) for row in data["all"]]),
    ])


def write_sheets(path, sheets):
    """``sheets`` is a list of (name, header, rows)."""
    count = len(sheets)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(1, count + 1)) + "</Types>")
        z.writestr("_rels/.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PKG}">'
                   f'<Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{_MAIN}" xmlns:r="{_REL}"><sheets>'
                   + "".join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _, _) in enumerate(sheets, start=1)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PKG}">'
                   + "".join(f'<Relationship Id="rId{i}" Type="{_REL}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, count + 1))
                   + f'<Relationship Id="rId{count + 1}" Type="{_REL}/styles" Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml", f'<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="{_MAIN}">'
                   '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
                   '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
                   '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                   '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                   '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                   '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs></styleSheet>')
        for i, (_, header, rows) in enumerate(sheets, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet(header, rows))
