"""A fake TallyPrime that speaks the real XML envelope.

This is the test double the whole suite runs against, and the thing
``scripts/dev_fake_tally.py`` serves so a developer can run the demo without
TallyPrime installed. It holds real state: post a voucher and the trial balance
moves. It also reproduces the failure modes that matter - unknown ledger,
duplicate voucher number - as genuine ``<LINEERROR>`` responses, because a fake
that only ever succeeds tests nothing.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import httpx
from lxml import etree

from tallyagent_tally.xml.quirks import from_tally_date


@dataclass(slots=True)
class FakeLedger:
    name: str
    parent: str
    opening_balance: Decimal = Decimal("0")
    gstin: str = ""
    state: str = ""
    master_id: str = ""


@dataclass(slots=True)
class FakeVoucher:
    master_id: str
    voucher_number: str
    voucher_type: str
    date: date | None
    party_name: str
    reference: str
    narration: str
    # (ledger_name, amount) in *our* convention: positive = debit
    lines: list[tuple[str, Decimal]] = field(default_factory=list)


class FakeTally:
    """In-memory Tally. Not thread-safe; one instance per test."""

    def __init__(self, company: str = "Demo Traders Pvt Ltd", version: str = "6.0.3") -> None:
        self.company = company
        self.companies = [company]
        self.version = version
        self.ledgers: dict[str, FakeLedger] = {}
        self.vouchers: list[FakeVoucher] = []
        self.requests: list[bytes] = []
        self._ids = itertools.count(1)
        self._numbers: dict[str, itertools.count[int]] = {}

    # --- seeding ------------------------------------------------------------

    def add_ledger(
        self,
        name: str,
        parent: str,
        opening_balance: Decimal | str = "0",
        gstin: str = "",
        state: str = "",
    ) -> FakeLedger:
        ledger = FakeLedger(
            name=name,
            parent=parent,
            opening_balance=Decimal(str(opening_balance)),
            gstin=gstin,
            state=state,
            master_id=str(next(self._ids)),
        )
        self.ledgers[name] = ledger
        return ledger

    def next_voucher_number(self, voucher_type: str) -> str:
        counter = self._numbers.setdefault(voucher_type, itertools.count(1))
        prefix = {"Sales": "S", "Purchase": "P", "Payment": "PY", "Receipt": "R"}.get(
            voucher_type, "J"
        )
        return f"{prefix}/{next(counter)}"

    # --- balances -----------------------------------------------------------

    def balance(self, ledger_name: str) -> Decimal:
        """Closing balance in our convention: positive = debit."""
        ledger = self.ledgers.get(ledger_name)
        total = ledger.opening_balance if ledger else Decimal("0")
        for voucher in self.vouchers:
            for name, amount in voucher.lines:
                if name == ledger_name:
                    total += amount
        return total

    # --- HTTP ---------------------------------------------------------------

    @property
    def transport(self) -> httpx.MockTransport:
        """Attach to a TallyClient without opening a socket."""
        return httpx.MockTransport(self._handle_httpx)

    def _handle_httpx(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                text=(
                    "<html><body>TallyPrime Server "
                    f"{self.version}</body></html>"
                ),
            )
        body = self.handle(request.content)
        return httpx.Response(200, content=body, headers={"Content-Type": "text/xml"})

    def handle(self, payload: bytes) -> bytes:
        """Route one ENVELOPE to the right handler."""
        self.requests.append(payload)
        root = etree.fromstring(payload)
        request_type = (root.findtext(".//TALLYREQUEST") or "").strip()
        if request_type == "Import Data":
            return self._handle_import(root)
        ident = (root.findtext(".//HEADER/ID") or "").strip()
        return self._handle_export(ident, root)

    # --- exports ------------------------------------------------------------

    def _handle_export(self, ident: str, root: etree._Element) -> bytes:
        if ident == "List of Companies":
            return self._collection(
                "COMPANY", [{"NAME": name} for name in self.companies]
            )
        if ident in ("List of Ledgers", "Ledger"):
            return self._collection(
                "LEDGER",
                [
                    {
                        "@NAME": ledger.name,
                        "NAME": ledger.name,
                        "PARENT": ledger.parent,
                        "OPENINGBALANCE": format(ledger.opening_balance, "f"),
                        "PARTYGSTIN": ledger.gstin,
                        "LEDSTATENAME": ledger.state,
                        "MASTERID": ledger.master_id,
                    }
                    for ledger in self.ledgers.values()
                ],
            )
        if ident == "Trial Balance":
            return self._collection(
                "LEDGER",
                [
                    {
                        "NAME": name,
                        "PARENT": ledger.parent,
                        "CLOSINGBALANCE": format(self.balance(name), "f"),
                    }
                    for name, ledger in self.ledgers.items()
                ],
            )
        if ident in ("Day Book", "Voucher Register", "Ledger Vouchers"):
            return self._collection("VOUCHER", self._voucher_rows(root))
        if ident in ("Bills Receivable", "Bills Payable"):
            return self._collection(
                "BILLS", self._outstanding_rows(receivable=ident == "Bills Receivable")
            )
        # Unknown report: Tally answers with an empty envelope, not an error.
        return self._collection("UNKNOWN", [])

    def _voucher_rows(self, root: etree._Element) -> list[dict[str, str]]:
        from_date = from_tally_date(root.findtext(".//SVFROMDATE") or "")
        to_date = from_tally_date(root.findtext(".//SVTODATE") or "")
        ledger_filter = (root.findtext(".//SVLEDGERNAME") or "").strip()
        rows: list[dict[str, str]] = []
        for voucher in self.vouchers:
            if from_date and voucher.date and voucher.date < from_date:
                continue
            if to_date and voucher.date and voucher.date > to_date:
                continue
            for name, amount in voucher.lines:
                if ledger_filter and name != ledger_filter:
                    continue
                rows.append(
                    {
                        "DATE": voucher.date.strftime("%Y%m%d") if voucher.date else "",
                        "VOUCHERTYPENAME": voucher.voucher_type,
                        "VOUCHERNUMBER": voucher.voucher_number,
                        "LEDGERNAME": name,
                        "PARTYLEDGERNAME": voucher.party_name,
                        "AMOUNT": format(amount, "f"),
                        "NARRATION": voucher.narration,
                        "REFERENCE": voucher.reference,
                        "MASTERID": voucher.master_id,
                    }
                )
        return rows

    def _outstanding_rows(self, receivable: bool) -> list[dict[str, str]]:
        parent = "Sundry Debtors" if receivable else "Sundry Creditors"
        rows: list[dict[str, str]] = []
        for name, ledger in self.ledgers.items():
            if ledger.parent != parent:
                continue
            balance = self.balance(name)
            if balance == 0:
                continue
            reference = ""
            bill_date = ""
            for voucher in self.vouchers:
                if voucher.party_name == name and voucher.reference:
                    reference = voucher.reference
                    bill_date = voucher.date.strftime("%Y%m%d") if voucher.date else ""
            rows.append(
                {
                    "NAME": reference or name,
                    "PARTYNAME": name,
                    "BILLDATE": bill_date,
                    "CLOSINGBALANCE": format(balance, "f"),
                }
            )
        return rows

    def _collection(self, tag: str, rows: list[dict[str, str]]) -> bytes:
        envelope = etree.Element("ENVELOPE")
        body = etree.SubElement(envelope, "BODY")
        data = etree.SubElement(body, "DATA")
        collection = etree.SubElement(data, "COLLECTION")
        for row in rows:
            element = etree.SubElement(collection, tag)
            for key, value in row.items():
                if key.startswith("@"):
                    element.set(key[1:], value)
                else:
                    etree.SubElement(element, key).text = value
        return etree.tostring(envelope, xml_declaration=False)

    # --- imports ------------------------------------------------------------

    def _handle_import(self, root: etree._Element) -> bytes:
        created = altered = 0
        errors: list[str] = []
        last_voucher_id = last_master_id = ""

        for ledger_el in root.iter("LEDGER"):
            name = ledger_el.findtext("NAME") or ledger_el.get("NAME") or ""
            if name in self.ledgers:
                errors.append(f"Ledger '{name}' already exists")
                continue
            ledger = self.add_ledger(
                name=name,
                parent=ledger_el.findtext("PARENT") or "",
                gstin=ledger_el.findtext("PARTYGSTIN") or "",
                state=ledger_el.findtext("LEDSTATENAME") or "",
            )
            last_master_id = ledger.master_id
            created += 1

        for voucher_el in root.iter("VOUCHER"):
            action = voucher_el.get("ACTION", "Create")
            lines: list[tuple[str, Decimal]] = []
            unknown: list[str] = []
            for entry in voucher_el.iter("ALLLEDGERENTRIES.LIST"):
                name = entry.findtext("LEDGERNAME") or ""
                if name not in self.ledgers:
                    unknown.append(name)
                    continue
                # Undo Tally's negation to get back to our convention.
                raw = entry.findtext("AMOUNT") or "0"
                lines.append((name, -Decimal(raw)))
            if unknown:
                errors.extend(
                    f"Ledger '{name}' does not exist in the company" for name in unknown
                )
                continue

            voucher_type = voucher_el.get("VCHTYPE") or (
                voucher_el.findtext("VOUCHERTYPENAME") or "Journal"
            )
            if action == "Alter":
                master_id = voucher_el.get("MASTERID") or (
                    voucher_el.findtext("MASTERID") or ""
                )
                existing = next(
                    (v for v in self.vouchers if v.master_id == master_id), None
                )
                if existing is None:
                    errors.append(f"Voucher with MASTERID '{master_id}' could not be found")
                    continue
                existing.lines = lines
                existing.date = from_tally_date(voucher_el.findtext("DATE") or "")
                existing.narration = voucher_el.findtext("NARRATION") or ""
                last_voucher_id = existing.master_id
                altered += 1
                continue

            number = voucher_el.findtext("VOUCHERNUMBER") or self.next_voucher_number(
                voucher_type
            )
            if any(
                v.voucher_number == number and v.voucher_type == voucher_type
                for v in self.vouchers
            ):
                errors.append(f"Duplicate voucher number '{number}'")
                continue
            voucher = FakeVoucher(
                master_id=str(next(self._ids)),
                voucher_number=number,
                voucher_type=voucher_type,
                date=from_tally_date(voucher_el.findtext("DATE") or ""),
                party_name=voucher_el.findtext("PARTYLEDGERNAME") or "",
                reference=voucher_el.findtext("REFERENCE") or "",
                narration=voucher_el.findtext("NARRATION") or "",
                lines=lines,
            )
            self.vouchers.append(voucher)
            last_voucher_id = voucher.master_id
            created += 1

        return self._import_response(
            created, altered, errors, last_voucher_id, last_master_id
        )

    def _import_response(
        self,
        created: int,
        altered: int,
        errors: list[str],
        last_voucher_id: str,
        last_master_id: str,
    ) -> bytes:
        envelope = etree.Element("ENVELOPE")
        body = etree.SubElement(envelope, "BODY")
        data = etree.SubElement(body, "DATA")
        result = etree.SubElement(data, "IMPORTRESULT")
        etree.SubElement(result, "CREATED").text = str(created)
        etree.SubElement(result, "ALTERED").text = str(altered)
        etree.SubElement(result, "IGNORED").text = "0"
        etree.SubElement(result, "EXCEPTIONS").text = str(len(errors))
        etree.SubElement(result, "LASTVCHID").text = last_voucher_id
        etree.SubElement(result, "LASTMASTERID").text = last_master_id
        for message in errors:
            etree.SubElement(result, "LINEERROR").text = message
        return etree.tostring(envelope, xml_declaration=False)


def seeded_demo(company: str = "Demo Traders Pvt Ltd") -> FakeTally:
    """The demo company every test and the demo script share."""
    tally = FakeTally(company=company)
    tally.add_ledger(
        "Acme Industries", "Sundry Debtors", gstin="27AAPFU0939F1ZV", state="Maharashtra"
    )
    tally.add_ledger(
        "Bharat Supplies", "Sundry Creditors", gstin="29AAGCB7383J1Z4", state="Karnataka"
    )
    tally.add_ledger("Sales - GST 18%", "Sales Accounts")
    tally.add_ledger("Purchase - GST 18%", "Purchase Accounts")
    tally.add_ledger("Output CGST", "Duties & Taxes")
    tally.add_ledger("Output SGST", "Duties & Taxes")
    tally.add_ledger("Output IGST", "Duties & Taxes")
    tally.add_ledger("Input CGST", "Duties & Taxes")
    tally.add_ledger("Input SGST", "Duties & Taxes")
    tally.add_ledger("Input IGST", "Duties & Taxes")
    tally.add_ledger("Bank - HDFC 1234", "Bank Accounts", opening_balance="250000.00")
    tally.add_ledger("Cash", "Cash-in-Hand", opening_balance="15000.00")
    return tally
