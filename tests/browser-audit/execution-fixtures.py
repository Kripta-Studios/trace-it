"""Generate synthetic expense evidence for the isolated browser audit."""

import json
import os
from pathlib import Path

import pymupdf
from openpyxl import Workbook
from PIL import Image, ImageDraw

ROOT = Path(os.environ.get("AUDIT_ARTIFACTS", "/home/kripta/trace-it-deep-evidence-20261018/execution"))
ROOT.mkdir(parents=True, exist_ok=True)

definition = {
    "name": "Travel expense audit 20261018",
    "description": "Synthetic employee travel expenses. Amount is the receipt total in its stated currency; never convert currencies. Missing required data must require human review.",
    "decision_types": [
        {"name": "REVIEW", "priority": 3, "requires_human": True},
        {"name": "REJECT", "priority": 2},
        {"name": "APPROVE", "priority": 1, "is_default": True},
    ],
    "symbols": [
        {"name": "receipt_id", "type": "text", "required": True, "extraction": {"labels": ["Receipt ID"]}},
        {"name": "employee", "type": "text", "required": True, "extraction": {"labels": ["Employee"]}},
        {"name": "amount", "type": "number", "required": True, "extraction": {"labels": ["Amount"]}},
        {"name": "currency", "type": "text", "required": True, "extraction": {"labels": ["Currency"]}},
    ],
    "rules": [
        {"text": "Reject the expense when amount is less than or equal to zero. Compare numeric amount with zero. This rule does not inspect currency or other receipts.", "type": "prohibition", "decision": "REJECT"},
        {"text": "Require human review when currency is not EUR, or when amount is greater than 150. Compare currency after trimming whitespace and converting to uppercase. No currency conversion and no source lookup.", "type": "requirement", "decision": "REVIEW"},
    ],
    "users": [],
}
(ROOT / "process.json").write_text(json.dumps(definition, indent=2))
cases = [
    ("01-approved", "T-001", "42.50", "EUR", "APPROVE"),
    ("02-high", "T-002", "180", "EUR", "REVIEW"),
    ("03-usd", "T-003", "60", "USD", "REVIEW"),
    ("04-zero", "T-004", "0", "EUR", "REJECT"),
    ("05-negative", "T-005", "-15", "EUR", "REJECT"),
    ("06-missing", "T-006", None, "EUR", "REVIEW"),
]
manifest = []
for name, receipt, amount, currency, expected in cases:
    lines = ["TRAVEL EXPENSE RECEIPT", f"Receipt ID: {receipt}", "Employee: Alex Example", "Date: 2026-10-17", f"Currency: {currency}"]
    if amount is not None:
        lines.append(f"Amount: {amount}")
    lines += ["Purpose: Airport transfer", "Synthetic test evidence; no payment should be made."]
    text = "\n".join(lines)
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((60, 70), text, fontsize=14)
    path = ROOT / f"{name}.pdf"
    pdf.save(path)
    pdf.close()
    manifest.append({"file": path.name, "expected": expected, "amount": amount, "currency": currency})

(ROOT / "07-html.html").write_text("<!doctype html><html><body><h1>Travel receipt</h1><p>Receipt ID: T-007</p><p>Employee: Alex Example</p><p>Amount: 17.50</p><p>Currency: EUR</p><p>Purpose: Train fare</p></body></html>")
(ROOT / "08-corrupt.pdf").write_bytes(b"%PDF-1.7\nThis is intentionally corrupted test data.")
(ROOT / "09-empty.pdf").write_bytes(b"")
(ROOT / "10-unsupported.csv").write_text("receipt_id,employee,amount,currency\nT-010,Alex Example,21,EUR\n")
workbook = Workbook()
sheet = workbook.active
sheet.title = "travel_rates"
sheet.append(["currency", "rate_to_eur"])
sheet.append(["EUR", 1])
sheet.append(["USD", 0.92])
workbook.save(ROOT / "11-rates.xlsx")
image = Image.new("RGB", (1000, 700), "white")
draw = ImageDraw.Draw(image)
draw.multiline_text((50, 50), "TRAVEL EXPENSE RECEIPT\nReceipt ID: T-012\nEmployee: Alex Example\nAmount: 22.00\nCurrency: EUR\nPurpose: Tram fare", fill="black", font_size=32, spacing=18)
image.save(ROOT / "12-image.png")
(ROOT / "15-image-local.png").write_bytes((ROOT / "12-image.png").read_bytes())
(ROOT / "13-zero-retry.pdf").write_bytes((ROOT / "04-zero.pdf").read_bytes())
(ROOT / "14-negative-retry.pdf").write_bytes((ROOT / "05-negative.pdf").read_bytes())
(ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2))
print(f"Generated {len(list(ROOT.iterdir()))} synthetic artifacts in {ROOT}")
