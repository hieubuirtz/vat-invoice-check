#!/usr/bin/env python3
"""Sort VAT invoices (PDF) by whether the buyer details are correct.

Reads every PDF in <root>/invoice-in, finds the invoice date and checks that the
expected buyer name and address appear on the invoice.

  * buyer correct   -> moved to <root>/yyyy-mm/   (month of the invoice date)
  * buyer incorrect -> moved to <root>/incorrect-invoice/
    (also used when the file can't be read or has no date; the reason is shown)

<root> is the folder this script lives in. Nothing is ever overwritten: if a
file with the same name already exists in the destination, the invoice is left
in invoice-in/ and reported as SKIPPED.

Usage:
    python3 sort_invoices.py             # move files
    python3 sort_invoices.py --dry-run   # show what would happen, move nothing

Requires: pdfplumber and/or pypdf (pip install -r requirements.txt).
"""

import argparse
import datetime as dt
import logging
import re
import shutil
import sys
import unicodedata
from pathlib import Path

logging.getLogger("pypdf").setLevel(logging.CRITICAL)  # no spam on corrupt PDFs

# ---------------------------------------------------------------- expected data
CORRECT_NAME = "AS WHITE AUSTRALIA PTY LTD"
CORRECT_ADDRESS = (
    "Level 3, 345 George Street, SYDNEY NSW 2000, Australia. ACN: 613328582"
)

INBOX_DIR = "invoice-in"
INCORRECT_DIR = "incorrect-invoice"

# --------------------------------------------------------------- text extraction


def check_dependencies():
    """Exit with install instructions if no PDF library is available.

    Without this, a missing library would look like an unreadable invoice and
    every file would be moved to incorrect-invoice/.
    """
    for module in ("pdfplumber", "pypdf"):
        try:
            __import__(module)
            return
        except ImportError:
            continue
    sys.exit(
        "No PDF library found. Install one (nothing was moved):\n"
        "    pip install -r requirements.txt"
    )


def extract_texts(path):
    """Return the PDF text as extracted by each available library.

    Libraries order text differently on multi-column layouts, so the checks
    below try every variant. Raises only if none of them could read the file.
    """
    texts, errors = [], []

    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            texts.append("\n".join((p.extract_text() or "") for p in pdf.pages))
    except Exception as exc:
        errors.append("pdfplumber: %s" % exc)

    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        texts.append("\n".join((p.extract_text() or "") for p in reader.pages))
    except Exception as exc:
        errors.append("pypdf: %s" % exc)

    if not texts:
        raise RuntimeError("; ".join(errors) or "no PDF library installed")
    return [t for t in texts if t.strip()]


# ------------------------------------------------------------------- normalising


def normalize(text):
    """Upper-case, strip accents and punctuation, collapse whitespace."""
    text = (text or "").replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^A-Za-z0-9]+", " ", text)
    return text.upper().strip()


def tokens(text):
    return normalize(text).split()


def contains_in_order(haystack, needle, max_gap_factor=3):
    """True if every `needle` token appears in `haystack`, in order.

    Exact consecutive match is tried first. If that fails, the needle may be
    spread out (e.g. a wrapped address with another column's text interleaved),
    provided the whole match fits in a span of len(needle) * max_gap_factor
    tokens. Every needle token must still be present, so a changed street
    number, name or ACN digit never matches.
    """
    n = len(needle)
    if not n:
        return False
    for start in range(len(haystack) - n + 1):
        if haystack[start:start + n] == needle:
            return True

    limit = n * max_gap_factor
    for start, tok in enumerate(haystack):
        if tok != needle[0]:
            continue
        pos, ok = start, True
        for want in needle[1:]:
            pos += 1
            while pos < len(haystack) and haystack[pos] != want:
                pos += 1
            if pos >= len(haystack) or pos - start >= limit:
                ok = False
                break
        if ok:
            return True
    return False


# ---------------------------------------------------------------------- buyer


def check_buyer(texts):
    """Return (name_found, address_found) across all extraction variants."""
    name_t, addr_t = tokens(CORRECT_NAME), tokens(CORRECT_ADDRESS)
    name_ok = addr_ok = False
    for text in texts:
        hay = tokens(text)
        name_ok = name_ok or contains_in_order(hay, name_t)
        addr_ok = addr_ok or contains_in_order(hay, addr_t)
    return name_ok, addr_ok


# Best-effort, for the error message only: what does the invoice say instead?
_BUYER_START = re.compile(
    r"^(?:(?:Họ\s*)?tên người mua|người mua|buyer|customer|bill to|sold to)",
    re.I,
)
_BUYER_END = re.compile(r"^(?:hình thức thanh toán|payment|stt\b|no\.|số tài khoản)", re.I)
_LABELS = [
    (re.compile(r"^(?:đơn vị|tên đơn vị|tên công ty|company(?:'s name)?|customer name)\b[^:：]*[:：]", re.I), "name"),
    (re.compile(r"^(?:địa chỉ|address)\b[^:：]*[:：]", re.I), "address"),
    (re.compile(r"^(?:mã số thuế|tax code|điện thoại|tel|căn cước|số tài khoản)\b[^:：]*[:：]?", re.I), "other"),
]


def describe_buyer(texts):
    """Guess the buyer name/address printed on the invoice (diagnostics only)."""
    for text in texts:
        found, in_block, current = {}, False, None
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if not in_block:
                in_block = bool(_BUYER_START.match(line))
                if not in_block:
                    continue
            elif _BUYER_END.match(line):
                break
            for pattern, field in _LABELS:
                m = pattern.match(line)
                if m:
                    current = field
                    found[current] = line[m.end():].strip()
                    break
            else:
                if current and current != "other":
                    found[current] = (found.get(current, "") + " " + line).strip()
        if found.get("name") or found.get("address"):
            return found.get("name") or "", found.get("address") or ""
    return "", ""


# ------------------------------------------------------------------------ date

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
         "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]
    )
}
_MONTHS_BY_ABBR = {name[:3]: num for name, num in _MONTHS.items()}
_MON_RE = "|".join(m[:3] for m in _MONTHS)  # JAN|FEB|... (full names match via [a-z]*)
_PAREN = r"(?:\([^)]*\))?"  # bilingual labels: "Ngày (Date)"
_DATE_LABEL = (
    r"(?:invoice\s*date|date\s*of\s*(?:issue|invoice)|issue\s*date|tax\s*invoice\s*date"
    r"|ngày\s*(?:hóa\s*đơn|lập|phát\s*hành)|ngày|date)"
)

# Tried in order; first match wins. Numeric dates are day-first (Vietnam/AU).
DATE_PATTERNS = [
    # "Ngày (Date) 03 tháng (month) 10 năm (year) 2026"
    re.compile(
        r"ngày\s*%s\s*(?P<d>\d{1,2})\s*tháng\s*%s\s*(?P<m>\d{1,2})\s*năm\s*%s\s*(?P<y>\d{4})"
        % (_PAREN, _PAREN, _PAREN),
        re.I,
    ),
    # "Invoice date: 03/10/2026", "Ngày (Date): 03-10-2026"
    re.compile(
        r"%s\s*%s\s*[:：]?\s*(?P<d>\d{1,2})\s*[/.\-]\s*(?P<m>\d{1,2})\s*[/.\-]\s*(?P<y>\d{4})"
        % (_DATE_LABEL, _PAREN),
        re.I,
    ),
    # "Date: 2026-10-03"
    re.compile(
        r"%s\s*%s\s*[:：]?\s*(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})"
        % (_DATE_LABEL, _PAREN),
        re.I,
    ),
    # "Invoice date: 3 October 2026"
    re.compile(
        r"%s\s*%s\s*[:：]?\s*(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m>%s)[a-z]*\.?,?\s+(?P<y>\d{4})"
        % (_DATE_LABEL, _PAREN, _MON_RE),
        re.I,
    ),
    # Last resort: signature stamp "Ngày ký: 03-10-2026"
    re.compile(
        r"ngày\s*ký\s*[:：]?\s*(?P<d>\d{1,2})\s*[/.\-]\s*(?P<m>\d{1,2})\s*[/.\-]\s*(?P<y>\d{4})",
        re.I,
    ),
]


def parse_date(texts):
    for pattern in DATE_PATTERNS:
        for text in texts:
            for m in pattern.finditer(text):
                month = m.group("m")
                month = _MONTHS_BY_ABBR.get(month.upper()[:3]) if not month.isdigit() else int(month)
                try:
                    year = int(m.group("y"))
                    if 2000 <= year <= 2100:
                        return dt.date(year, month, int(m.group("d")))
                except (ValueError, TypeError):
                    continue
    return None


# -------------------------------------------------------------------- processing


def classify(path):
    """Return (destination folder name, status detail). Never touches the file."""
    try:
        texts = extract_texts(path)
    except Exception as exc:
        return INCORRECT_DIR, "could not read PDF (%s)" % exc
    if not texts:
        return INCORRECT_DIR, "no extractable text (scanned image?)"

    name_ok, addr_ok = check_buyer(texts)
    problems = []
    if not (name_ok and addr_ok):
        seen_name, seen_addr = describe_buyer(texts)
        if not name_ok:
            problems.append(
                "buyer name not %r%s"
                % (CORRECT_NAME, " (invoice says %r)" % seen_name if seen_name else "")
            )
        if not addr_ok:
            problems.append(
                "buyer address does not match%s"
                % (" (invoice says %r)" % seen_addr if seen_addr else "")
            )
    if problems:
        return INCORRECT_DIR, "; ".join(problems)

    date = parse_date(texts)
    if date is None:
        return INCORRECT_DIR, "buyer OK but invoice date not found"
    return date.strftime("%Y-%m"), "buyer OK, invoice date %s" % date.isoformat()


def main():
    ap = argparse.ArgumentParser(description="Sort VAT invoices by buyer validity.")
    ap.add_argument("--dry-run", action="store_true", help="report only, move nothing")
    args = ap.parse_args()
    check_dependencies()

    root = Path(__file__).resolve().parent
    inbox = root / INBOX_DIR
    if not inbox.is_dir():
        sys.exit("Inbox folder not found: %s" % inbox)

    all_files = sorted(p for p in inbox.iterdir() if p.is_file() and not p.name.startswith("."))
    pdfs = [p for p in all_files if p.suffix.lower() == ".pdf"]
    for p in all_files:
        if p not in pdfs:
            print("[IGNORED  ] %s (not a PDF, left in %s/)" % (p.name, INBOX_DIR))
    if not pdfs:
        print("No PDF invoices in %s" % inbox)
        return

    counts = {"MOVED": 0, "INCORRECT": 0, "SKIPPED": 0}
    for path in pdfs:
        dest_name, detail = classify(path)
        dest_dir = root / dest_name
        dest = dest_dir / path.name
        incorrect = dest_name == INCORRECT_DIR

        if dest.exists():
            counts["SKIPPED"] += 1
            print("[SKIPPED  ] %s -> %s/ already has a file with this name, not overwritten (%s)"
                  % (path.name, dest_name, detail))
            continue

        if not args.dry_run:
            dest_dir.mkdir(exist_ok=True)
            shutil.move(str(path), str(dest))
        counts["INCORRECT" if incorrect else "MOVED"] += 1
        print("[%-9s] %s -> %s/  (%s)" % ("INCORRECT" if incorrect else "OK", path.name, dest_name, detail))

    print(
        "\n%d PDF(s)%s: %d correct, %d incorrect, %d skipped (name already exists)."
        % (len(pdfs), " [dry run, nothing moved]" if args.dry_run else "",
           counts["MOVED"], counts["INCORRECT"], counts["SKIPPED"])
    )


if __name__ == "__main__":
    main()
