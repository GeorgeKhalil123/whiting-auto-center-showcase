"""Invoice printing — render the receipt to a PDF and open it in the OS viewer.

Layout mirrors the existing on-screen receipt: shop header block, a details
grid (invoice #, date, serviced-by, customer, vehicle, mileage), a
work/qty/taxed/amount table, subtotal / tax / total, and the thank-you footer.

Primary path uses reportlab. If reportlab is unavailable we fall back to an
HTML receipt opened in the default browser, so printing always works.

The header below is a FICTIONAL shop. The production build prints the client's
own name, address and phone number, which are not part of this repository.
"""
from __future__ import annotations

import html
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SHOP_NAME = "SAMPLE AUTO REPAIR"
SHOP_ADDRESS = ["100 Example Street", "Anytown, ST 00000", "(555) 010-0000"]
FOOTER = "THANK YOU FOR YOUR BUSINESS!"
FOOTER_SUB = f"Make all checks payable to {SHOP_NAME}"


def _money(n: Any) -> str:
    return f"${float(n):,.2f}"


def _rate_label(invoice: dict, name: str, rate_key: str, label_key: str) -> str:
    """'Tax (6.625%)' — or plain 'Tax' when the rate was zero/absent."""
    label = invoice.get(label_key)
    if not label:
        rate = invoice.get(rate_key)
        label = f"{float(rate) * 100:g}%" if rate is not None else ""
    if not label or label.startswith("0%"):
        return name
    return f"{name} ({label})"


def _tax_label(invoice: dict) -> str:
    return _rate_label(invoice, "Tax", "taxRate", "taxRateLabel")


def _cc_label(invoice: dict) -> str:
    return _rate_label(invoice, "Card fee", "ccFeeRate", "ccFeeRateLabel")


def _has_cc_fee(invoice: dict) -> bool:
    """Only show the fee row when one was actually charged."""
    return bool(float(invoice.get("ccFee") or 0))


def _open_file(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def render(invoice: dict, dest: Path | str | None = None) -> Path:
    """Render `invoice` (from services.get_invoice) to a PDF, or HTML fallback.

    `dest` defaults to a file in the OS temp dir; the demo passes its own.
    """
    try:
        return _render_pdf(invoice, dest)
    except ImportError:
        return _render_html(invoice, dest)


def render_and_open(invoice: dict) -> Path:
    """Render `invoice` to a file and open it in the OS viewer to print."""
    path = render(invoice)
    _open_file(path)
    return path


def _out_path(no: Any, suffix: str, dest: Path | str | None = None) -> Path:
    if dest is not None:
        return Path(dest).with_suffix(suffix)
    return Path(tempfile.gettempdir()) / f"ShopLedger-Invoice-{no}{suffix}"


# ---- PDF (reportlab) ---------------------------------------------------------

def _render_pdf(invoice: dict, dest: Path | str | None = None) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import (SimpleDocTemplate, Spacer, Table, TableStyle,
                                    Paragraph)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER

    path = _out_path(invoice["no"], ".pdf", dest)
    doc = SimpleDocTemplate(str(path), pagesize=letter,
                            leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.7 * inch, bottomMargin=0.7 * inch)
    styles = getSampleStyleSheet()
    center = ParagraphStyle("center", parent=styles["Normal"], alignment=TA_CENTER)
    header = ParagraphStyle("shop", parent=styles["Title"], alignment=TA_CENTER,
                            fontSize=22, leading=26, spaceAfter=2)
    sub = ParagraphStyle("addr", parent=styles["Normal"], alignment=TA_CENTER,
                         fontSize=10, leading=14, textColor=colors.HexColor("#333333"))

    story: list = [Paragraph(SHOP_NAME, header),
                   Paragraph("<br/>".join(SHOP_ADDRESS), sub),
                   Spacer(1, 16)]

    # Details grid
    details = [
        ["INVOICE", str(invoice["no"]), "DATE", invoice.get("date", "")],
        ["SERVICED BY", invoice.get("servicedBy") or "—", "MILEAGE",
         str(invoice.get("mileage")) if invoice.get("mileage") is not None else "—"],
        ["CUSTOMER", invoice.get("customer", "—"), "PHONE", invoice.get("phone", "—")],
        ["VEHICLE", invoice.get("vehicle", "—"), "PLATE", invoice.get("plate", "—")],
    ]
    dt = Table(details, colWidths=[1.1 * inch, 2.4 * inch, 1.0 * inch, 2.4 * inch])
    dt.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
        ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#555555")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#bbbbbb")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [dt, Spacer(1, 16)]

    # Line-item table
    rows: list[list[str]] = [["WORK", "QTY", "TAX", "AMOUNT"]]
    for it in invoice.get("items", []):
        rows.append([
            it["name"],
            str(it["qty"]),
            "✓" if it.get("taxed") else "",
            _money(it["lineTotal"]),
        ])
    # pad to a minimum of 8 body rows for a receipt look
    while len(rows) - 1 < 8:
        rows.append(["", "", "", ""])

    it_table = Table(rows, colWidths=[4.0 * inch, 0.7 * inch, 0.6 * inch, 1.6 * inch], repeatRows=1)
    style = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111111")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("ALIGN", (2, 0), (2, -1), "CENTER"),
        ("ALIGN", (3, 0), (3, -1), "RIGHT"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e5e5")),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    for i in range(1, len(rows)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f4f4f5")))
    it_table.setStyle(TableStyle(style))
    story += [it_table, Spacer(1, 10)]

    # Totals
    totals = [
        ["Subtotal", _money(invoice["subtotal"])],
        [_tax_label(invoice), _money(invoice["tax"])],
    ]
    if _has_cc_fee(invoice):
        totals.append([_cc_label(invoice), _money(invoice["ccFee"])])
    totals.append(["TOTAL", _money(invoice["total"])])
    grand = len(totals) - 1
    tt = Table(totals, colWidths=[5.3 * inch, 1.6 * inch])
    tt.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, grand), (-1, grand), "Helvetica-Bold"),
        ("FONTSIZE", (0, grand), (-1, grand), 12),
        ("LINEABOVE", (0, grand), (-1, grand), 1, colors.HexColor("#111111")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [tt, Spacer(1, 8)]

    comments = invoice.get("comments") or ""
    if comments.strip():
        # Paragraph collapses newlines, so keep the typed line breaks.
        body = html.escape(comments).replace("\n", "<br/>")
        story += [Paragraph(f"<b>Comments:</b> {body}", styles["Normal"]),
                  Spacer(1, 10)]

    footer = ParagraphStyle("footer", parent=center, fontSize=13, leading=16,
                            spaceBefore=14, fontName="Helvetica-Bold")
    story += [Paragraph(FOOTER, footer),
              Paragraph(FOOTER_SUB, sub)]

    doc.build(story)
    return path


# ---- HTML fallback -----------------------------------------------------------

def _render_html(invoice: dict, dest: Path | str | None = None) -> Path:
    path = _out_path(invoice["no"], ".html", dest)
    rows = "".join(
        f"<tr><td>{html.escape(it['name'])}</td>"
        f"<td style='text-align:center'>{it['qty']}</td>"
        f"<td style='text-align:center'>{'✓' if it.get('taxed') else ''}</td>"
        f"<td style='text-align:right'>{_money(it['lineTotal'])}</td></tr>"
        for it in invoice.get("items", [])
    )
    mileage = invoice.get("mileage")
    cc_row = (f"<tr><td>{html.escape(_cc_label(invoice))}</td>"
              f"<td>{_money(invoice['ccFee'])}</td></tr>") if _has_cc_fee(invoice) else ""
    comments = (invoice.get("comments") or "").strip()
    notes_block = (f'<div class="notes"><b>Other comments / recommendations</b>'
                   f'<div>{html.escape(comments)}</div></div>') if comments else ""
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Invoice #{invoice['no']}</title>
<style>
 body{{font-family:Arial,Helvetica,sans-serif;max-width:760px;margin:24px auto;color:#111;}}
 h1{{text-align:center;letter-spacing:.5px;margin:0;}}
 .addr{{text-align:center;color:#333;font-size:13px;line-height:1.5;margin:4px 0 18px;}}
 table{{width:100%;border-collapse:collapse;margin:12px 0;}}
 th,td{{border:1px solid #ccc;padding:7px 10px;font-size:14px;}}
 thead th{{background:#111;color:#fff;}}
 tbody tr:nth-child(even){{background:#f4f4f5;}}
 .totals td{{border:none;text-align:right;}}
 .grand{{font-weight:800;font-size:17px;border-top:2px solid #111;}}
 .notes{{margin-top:16px;font-size:13px;}}
 .notes>div{{border:1px solid #ccc;padding:10px 12px;margin-top:5px;white-space:pre-wrap;line-height:1.5;color:#444;min-height:52px;}}
 .foot{{text-align:center;font-weight:800;margin-top:22px;letter-spacing:.5px;}}
 .foot small{{display:block;color:#555;font-weight:400;margin-top:4px;}}
 @media print {{ button {{ display:none }} }}
</style></head><body onload="window.print()">
 <h1>{SHOP_NAME}</h1>
 <div class="addr">{'<br>'.join(SHOP_ADDRESS)}</div>
 <table>
   <tr><td><b>Invoice</b> #{invoice['no']}</td><td><b>Date</b> {html.escape(invoice.get('date',''))}</td>
       <td><b>Serviced by</b> {html.escape(invoice.get('servicedBy') or '—')}</td></tr>
   <tr><td><b>Customer</b> {html.escape(invoice.get('customer','—'))}</td>
       <td><b>Vehicle</b> {html.escape(invoice.get('vehicle','—'))}</td>
       <td><b>Mileage</b> {mileage if mileage is not None else '—'}</td></tr>
 </table>
 <table><thead><tr><th>WORK</th><th>QTY</th><th>TAX</th><th>AMOUNT</th></tr></thead>
 <tbody>{rows}</tbody></table>
 <table class="totals">
   <tr><td>Subtotal</td><td style="width:130px">{_money(invoice['subtotal'])}</td></tr>
   <tr><td>{html.escape(_tax_label(invoice))}</td><td>{_money(invoice['tax'])}</td></tr>
   {cc_row}
   <tr class="grand"><td>TOTAL</td><td>{_money(invoice['total'])}</td></tr>
 </table>
 {notes_block}
 <div class="foot">{FOOTER}<small>{FOOTER_SUB}</small></div>
</body></html>"""
    path.write_text(doc, encoding="utf-8")
    return path
