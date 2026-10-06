# VAT Invoice Check

A small Python script that sorts VAT invoices (PDF) by whether the **buyer details** on them are correct.

For every PDF in `invoice-in/` it finds the invoice date and checks the buyer name and address against the expected values:

| Result | Destination |
| --- | --- |
| Buyer name and address correct | `yyyy-mm/` (month of the invoice date) |
| Buyer incorrect, PDF unreadable, or no date found | `incorrect-invoice/` (the reason is printed) |

## Expected buyer

Set at the top of [`sort_invoices.py`](sort_invoices.py):

```python
CORRECT_NAME = "AS WHITE AUSTRALIA PTY LTD"
CORRECT_ADDRESS = "Level 3, 345 George Street, SYDNEY NSW 2000, Australia. ACN: 613328582"
```

## Usage

```bash
pip install pdfplumber pypdf

python3 sort_invoices.py             # sort the invoices
python3 sort_invoices.py --dry-run   # show what would happen, move nothing
```

Folders are created next to the script:

```
invoice-in/          drop invoices here
2026-10/             correct invoices, by invoice month
incorrect-invoice/   invoices that need attention
sort_invoices.py
```

## How it works

- **Buyer check:** looks for the expected name and full address (including the ACN) anywhere on the invoice. Case, accents, punctuation and line wraps are ignored, so it does not depend on one invoice layout. Any changed word or digit, such as a different street number or ACN, counts as a mismatch. If the address is wrapped or interleaved with another column, it still matches as long as every word is present in order.
- **Text extraction:** tries both `pdfplumber` and `pypdf`, because they order multi-column text differently.
- **Dates:** reads formats such as `Ngày 03 tháng 10 năm 2026`, `Invoice date: 03/10/2026`, `2026-10-03` and `3 October 2026`. Numeric dates are read day first. The signature date is used only as a last resort.
- **No overwrite:** if the destination already has a file with the same name, the invoice stays in `invoice-in/` and is reported as `SKIPPED`.
- **Non-PDF files** are ignored and left in `invoice-in/`.

## Limitations

- Scanned (image-only) PDFs have no text to read and go to `incorrect-invoice/`; there is no OCR.
- Because the check looks for the correct text anywhere on the page, an invoice for a different buyer that also prints the correct name and address elsewhere would pass.
