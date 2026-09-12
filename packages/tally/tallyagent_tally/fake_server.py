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
class FakeStockItem:
    name: str
    parent: str = ""
    base_units: str = "Nos"
    hsn_code: str = ""
    opening_quantity: Decimal = Decimal("0")
    opening_rate: Decimal = Decimal("0")
    master_id: str = ""


@dataclass(slots=True)
class FakeStockMove:
    """One inventory line on a voucher, in *our* convention (inward positive)."""

    stock_item: str
    quantity: Decimal
    rate: Decimal
    amount: Decimal
    godown: str = ""


@dataclass(slots=True)
class FakeVoucher:
    master_id: str
    voucher_number: str
    voucher_type: str
    date: date | None
    party_name: str
    reference: str
    narration: str
    #: The identity the caller assigned via REMOTEID, if any. Real Tally will
    #: only amend or delete a voucher addressed by one of these; addressing it
    #: by the MASTERID *it* assigned silently creates a duplicate instead.
    remote_id: str = ""
    # (ledger_name, amount) in *our* convention: positive = debit
    lines: list[tuple[str, Decimal]] = field(default_factory=list)
    #: ledger name -> [(category, cost centre, amount)] in our convention.
    costs: dict[str, list[tuple[str, str, Decimal]]] = field(default_factory=dict)
    #: Stock movements on this voucher, if any.
    stock: list[FakeStockMove] = field(default_factory=list)
    #: ledger name -> [(bill reference, bill type, amount)], as imported. Real
    #: Tally echoes back the allocations it was given, so the fake must too:
    #: inventing "On Account" for everything hid the bill-wise path entirely.
    bills: dict[str, list[tuple[str, str, Decimal]]] = field(default_factory=dict)


class FakeTally:
    """In-memory Tally. Not thread-safe; one instance per test."""

    def __init__(
        self,
        company: str = "Demo Traders Pvt Ltd",
        version: str = "6.0.3",
        companies: list[str] | None = None,
        edu: bool = False,
        supports_company_create: bool = True,
    ) -> None:
        self.company = company
        # ``companies=[]`` reproduces a fresh install sitting on Select Company,
        # which is the state Stage 14 bootstraps out of.
        self.companies = [company] if companies is None else list(companies)
        self.version = version
        # Educational mode only accepts vouchers on these days of the month.
        self.edu = edu
        # TallyPrime 1.1.7.1 refuses company creation over XML. Flipping this
        # off reproduces that refusal, so both paths are testable.
        self.supports_company_create = supports_company_create
        self.ledgers: dict[str, FakeLedger] = {}
        self.stock_items: dict[str, FakeStockItem] = {}
        self.units: dict[str, str] = {}
        self.godowns: dict[str, str] = {}
        self.cost_categories: set[str] = {"Primary Cost Category"}
        self.cost_centres: dict[str, str] = {}
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
            # Real Tally's plain ledger collection returns names and nothing
            # else. Reproducing that is the point: code that assumes richer
            # rows must fail here, not in front of a client.
            return self._collection(
                "LEDGER",
                [{"@NAME": ledger.name} for ledger in self.ledgers.values()],
            )
        if self._is_tdl_over(root, "ledger"):
            return self._collection("LEDGER", self._ledger_rows(root))
        if self._is_tdl_over(root, "voucher"):
            return self._voucher_collection(root)
        if self._is_tdl_over(root, "stockitem"):
            return self._collection(
                "STOCKITEM",
                [
                    {
                        "@NAME": item.name,
                        "NAME": item.name,
                        "PARENT": item.parent,
                        "BASEUNITS": item.base_units,
                        "HSNCODE": item.hsn_code,
                        "OPENINGBALANCE": (
                            f"{item.opening_quantity} {item.base_units}"
                            if item.opening_quantity
                            else ""
                        ),
                        "OPENINGRATE": (
                            f"{item.opening_rate}/{item.base_units}"
                            if item.opening_rate
                            else ""
                        ),
                        "MASTERID": item.master_id,
                    }
                    for item in self.stock_items.values()
                ],
            )
        if self._is_tdl_over(root, "unit"):
            return self._collection(
                "UNIT",
                [{"@NAME": name, "NAME": name, "FORMALNAME": formal}
                 for name, formal in self.units.items()],
            )
        if self._is_tdl_over(root, "costcategory"):
            return self._collection(
                "COSTCATEGORY",
                [{"@NAME": name, "NAME": name} for name in sorted(self.cost_categories)],
            )
        if self._is_tdl_over(root, "costcentre"):
            return self._collection(
                "COSTCENTRE",
                [
                    {"@NAME": name, "NAME": name, "PARENT": "", "CATEGORY": category}
                    for name, category in sorted(self.cost_centres.items())
                ],
            )
        if self._is_tdl_over(root, "godown"):
            return self._collection(
                "GODOWN",
                [{"@NAME": name, "NAME": name, "PARENT": parent}
                 for name, parent in self.godowns.items()],
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

    def _is_tdl_over(self, root: etree._Element, type_name: str) -> bool:
        """Does this request carry a TDL collection over ``type_name``?

        Matched on the TDL body rather than the collection name, because the
        name is the caller's to choose.
        """
        for collection in root.iter("COLLECTION"):
            if (collection.findtext("TYPE") or "").strip().lower() == type_name:
                return True
        return False

    def _voucher_collection(self, root: etree._Element) -> bytes:
        """Vouchers in the shape real Tally returns them.

        Nested ``ALLLEDGERENTRIES.LIST`` children, and amounts in *Tally's*
        convention (negated), because that is what the adapter has to flip.
        Emitting our convention here would hide the conversion bug we would
        then ship.
        """
        from_date = from_tally_date(root.findtext(".//SVFROMDATE") or "")
        to_date = from_tally_date(root.findtext(".//SVTODATE") or "")

        envelope = etree.Element("ENVELOPE")
        body = etree.SubElement(envelope, "BODY")
        data = etree.SubElement(body, "DATA")
        collection = etree.SubElement(data, "COLLECTION")

        for voucher in self.vouchers:
            if from_date and voucher.date and voucher.date < from_date:
                continue
            if to_date and voucher.date and voucher.date > to_date:
                continue
            element = etree.SubElement(
                collection,
                "VOUCHER",
                attrib={
                    "VCHTYPE": voucher.voucher_type,
                    "REMOTEID": voucher.remote_id or f"fake-guid-{voucher.master_id}",
                },
            )
            etree.SubElement(element, "DATE").text = (
                voucher.date.strftime("%Y%m%d") if voucher.date else ""
            )
            etree.SubElement(element, "VOUCHERTYPENAME").text = voucher.voucher_type
            etree.SubElement(element, "VOUCHERNUMBER").text = voucher.voucher_number
            etree.SubElement(element, "PARTYLEDGERNAME").text = voucher.party_name
            etree.SubElement(element, "REFERENCE").text = voucher.reference
            etree.SubElement(element, "NARRATION").text = voucher.narration
            etree.SubElement(element, "MASTERID").text = voucher.master_id
            if voucher.remote_id:
                element.set("REMOTEID", voucher.remote_id)
                etree.SubElement(element, "REMOTEID").text = voucher.remote_id
            for name, amount in voucher.lines:
                entry = etree.SubElement(element, "ALLLEDGERENTRIES.LIST")
                etree.SubElement(entry, "LEDGERNAME").text = name
                etree.SubElement(entry, "ISDEEMEDPOSITIVE").text = (
                    "Yes" if amount > 0 else "No"
                )
                etree.SubElement(entry, "AMOUNT").text = format(-amount, "f")
                for category, centre, cost_amount in voucher.costs.get(name, []):
                    cat = etree.SubElement(entry, "CATEGORYALLOCATIONS.LIST")
                    etree.SubElement(cat, "CATEGORY").text = category
                    cc = etree.SubElement(cat, "COSTCENTREALLOCATIONS.LIST")
                    etree.SubElement(cc, "NAME").text = centre
                    etree.SubElement(cc, "AMOUNT").text = format(-cost_amount, "f")
                # Real Tally nests further here; the parser must cope with it.
                allocations = voucher.bills.get(name)
                if allocations:
                    for reference, bill_type, bill_amount in allocations:
                        bill = etree.SubElement(entry, "BILLALLOCATIONS.LIST")
                        etree.SubElement(bill, "NAME").text = reference
                        etree.SubElement(bill, "BILLTYPE").text = bill_type
                        etree.SubElement(bill, "AMOUNT").text = format(
                            -bill_amount, "f"
                        )
                else:
                    bill = etree.SubElement(entry, "BILLALLOCATIONS.LIST")
                    etree.SubElement(bill, "BILLTYPE").text = "On Account"

            for move in voucher.stock:
                inv = etree.SubElement(element, "ALLINVENTORYENTRIES.LIST")
                etree.SubElement(inv, "STOCKITEMNAME").text = move.stock_item
                etree.SubElement(inv, "ISDEEMEDPOSITIVE").text = (
                    "Yes" if move.quantity > 0 else "No"
                )
                unit = (
                    self.stock_items[move.stock_item].base_units
                    if move.stock_item in self.stock_items
                    else ""
                )
                etree.SubElement(inv, "RATE").text = f"{move.rate}/{unit}"
                # Tally's convention: outward positive, inward negative.
                signed = move.amount if move.quantity < 0 else -move.amount
                etree.SubElement(inv, "AMOUNT").text = format(signed, "f")
                etree.SubElement(inv, "ACTUALQTY").text = f"{abs(move.quantity)} {unit}"
                etree.SubElement(inv, "BILLEDQTY").text = f"{abs(move.quantity)} {unit}"
                batch = etree.SubElement(inv, "BATCHALLOCATIONS.LIST")
                etree.SubElement(batch, "GODOWNNAME").text = (
                    move.godown or "Main Location"
                )
                etree.SubElement(batch, "AMOUNT").text = format(signed, "f")
        return etree.tostring(envelope, xml_declaration=False)

    def _ledger_rows(self, root: etree._Element) -> list[dict[str, str]]:
        """Only the fields the request asked for in <FETCH>.

        Real Tally returns exactly what was fetched, so a missing field in the
        FETCH list must be missing here too - otherwise the fake quietly
        supplies data production will not have.
        """
        fetch: set[str] = set()
        for element in root.iter("FETCH"):
            fetch |= {
                part.strip().upper() for part in (element.text or "").split(",") if part.strip()
            }

        available = {
            "NAME": lambda lg: lg.name,
            "PARENT": lambda lg: lg.parent,
            "OPENINGBALANCE": lambda lg: format(lg.opening_balance, "f"),
            "PARTYGSTIN": lambda lg: lg.gstin,
            "LEDSTATENAME": lambda lg: lg.state,
            "MASTERID": lambda lg: lg.master_id,
        }
        rows: list[dict[str, str]] = []
        for ledger in self.ledgers.values():
            row: dict[str, str] = {"@NAME": ledger.name}
            for field_name, read in available.items():
                if field_name in fetch:
                    value = read(ledger)
                    if value:
                        row[field_name] = value
            rows.append(row)
        return rows

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
                        "REMOTEID": voucher.remote_id,
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

        company_elements = list(root.iter("COMPANY"))
        if company_elements:
            return self._handle_company_import(root, company_elements)

        # Real Tally resolves a current company before any import. With none
        # loaded it refuses, and the error text is verbatim from TallyPrime.
        target = (root.findtext(".//SVCURRENTCOMPANY") or "").strip()
        if not target and not self.companies:
            return self._import_response(0, 0, ["Could not find Company ''"], "", "")

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

        for unit_el in root.iter("UNIT"):
            name = unit_el.findtext("NAME") or unit_el.get("NAME") or ""
            if name and name not in self.units:
                self.units[name] = unit_el.findtext("FORMALNAME") or name
                created += 1

        for category_el in root.iter("COSTCATEGORY"):
            name = category_el.findtext("NAME") or category_el.get("NAME") or ""
            if name and name not in self.cost_categories:
                self.cost_categories.add(name)
                created += 1

        for centre_el in root.iter("COSTCENTRE"):
            name = centre_el.findtext("NAME") or centre_el.get("NAME") or ""
            category = centre_el.findtext("CATEGORY") or "Primary Cost Category"
            if not name or name in self.cost_centres:
                continue
            if category not in self.cost_categories:
                # Same refusal as the real thing: a category created in the same
                # envelope is not yet resolvable.
                errors.append(f"Cost Category '{category}' does not exist")
                continue
            self.cost_centres[name] = category
            created += 1

        for godown_el in root.iter("GODOWN"):
            name = godown_el.findtext("NAME") or godown_el.get("NAME") or ""
            if name and name not in self.godowns:
                self.godowns[name] = godown_el.findtext("PARENT") or ""
                created += 1

        for item_el in root.iter("STOCKITEM"):
            name = item_el.findtext("NAME") or item_el.get("NAME") or ""
            if not name:
                continue
            if name in self.stock_items:
                errors.append(f"Stock item \u0027{name}\u0027 already exists")
                continue
            item = FakeStockItem(
                name=name,
                parent=item_el.findtext("PARENT") or "",
                base_units=item_el.findtext("BASEUNITS") or "Nos",
                hsn_code=item_el.findtext("HSNCODE") or "",
                opening_quantity=_quantity(item_el.findtext("OPENINGBALANCE")),
                opening_rate=_rate(item_el.findtext("OPENINGRATE")),
                master_id=str(next(self._ids)),
            )
            self.stock_items[name] = item
            last_master_id = item.master_id
            created += 1

        for voucher_el in root.iter("VOUCHER"):
            action = voucher_el.get("ACTION", "Create")
            lines: list[tuple[str, Decimal]] = []
            unknown: list[str] = []
            bills: dict[str, list[tuple[str, str, Decimal]]] = {}
            costs: dict[str, list[tuple[str, str, Decimal]]] = {}
            for entry in voucher_el.iter("ALLLEDGERENTRIES.LIST"):
                name = entry.findtext("LEDGERNAME") or ""
                if name not in self.ledgers:
                    unknown.append(name)
                    continue
                # Undo Tally's negation to get back to our convention.
                raw = entry.findtext("AMOUNT") or "0"
                lines.append((name, -Decimal(raw)))
                for allocation in entry.iter("BILLALLOCATIONS.LIST"):
                    reference = allocation.findtext("NAME") or ""
                    if not reference:
                        continue
                    bills.setdefault(name, []).append(
                        (
                            reference,
                            allocation.findtext("BILLTYPE") or "New Ref",
                            -Decimal(allocation.findtext("AMOUNT") or "0"),
                        )
                    )
                for category in entry.iter("CATEGORYALLOCATIONS.LIST"):
                    category_name = (
                        category.findtext("CATEGORY") or "Primary Cost Category"
                    )
                    for centre in category.iter("COSTCENTREALLOCATIONS.LIST"):
                        centre_name = centre.findtext("NAME") or ""
                        if not centre_name:
                            continue
                        if centre_name not in self.cost_centres:
                            unknown.append(f"cost centre {centre_name}")
                            continue
                        costs.setdefault(name, []).append(
                            (
                                category_name,
                                centre_name,
                                -Decimal(centre.findtext("AMOUNT") or "0"),
                            )
                        )
            stock: list[FakeStockMove] = []
            for inv in voucher_el.iter("INVENTORYALLOCATIONS.LIST"):
                item_name = inv.findtext("STOCKITEMNAME") or ""
                if item_name and item_name not in self.stock_items:
                    unknown.append(f"stock item {item_name}")
                    continue
                # Tally negates inventory amounts like ledger ones, and reports
                # quantity unsigned; undo both to get our convention back.
                amount = -Decimal(inv.findtext("AMOUNT") or "0")
                quantity = _quantity(inv.findtext("ACTUALQTY"))
                stock.append(
                    FakeStockMove(
                        stock_item=item_name,
                        quantity=quantity if amount > 0 else -quantity,
                        rate=_rate(inv.findtext("RATE")),
                        amount=abs(amount),
                        godown=next(
                            (
                                b.findtext("GODOWNNAME") or ""
                                for b in inv.iter("BATCHALLOCATIONS.LIST")
                            ),
                            "",
                        ),
                    )
                )

            if unknown and voucher_el.get("ACTION", "Create") != "Delete":
                errors.extend(
                    f"Ledger '{name}' does not exist in the company"
                    if not name.startswith("stock item ")
                    else f"Stock item '{name[11:]}' does not exist in the company"
                    for name in unknown
                )
                continue

            voucher_type = voucher_el.get("VCHTYPE") or (
                voucher_el.findtext("VOUCHERTYPENAME") or "Journal"
            )
            # Real Tally only honours an identity the caller supplied. The
            # MASTERID it assigned itself is not usable for Alter or Delete on
            # TallyPrime 1.x, so the fake matches on REMOTEID and nothing else.
            remote_id = voucher_el.get("REMOTEID") or (
                voucher_el.findtext("REMOTEID") or ""
            )

            if action == "Delete":
                target = next(
                    (v for v in self.vouchers if remote_id and v.remote_id == remote_id),
                    None,
                )
                if target is None:
                    errors.append(
                        "Voucher does not exist!"
                        if remote_id
                        else "Cannot delete unnamed object: VOUCHER!"
                    )
                    continue
                self.vouchers.remove(target)
                continue

            if action == "Alter":
                existing = next(
                    (v for v in self.vouchers if remote_id and v.remote_id == remote_id),
                    None,
                )
                if existing is None:
                    # What real Tally does, and why it is dangerous: an Alter
                    # that matches nothing is not an error, it silently creates
                    # a NEW voucher. Reproduced so the adapter's expect_altered
                    # guard against it is actually exercised.
                    voucher = FakeVoucher(
                        master_id=str(next(self._ids)),
                        voucher_number=self.next_voucher_number(voucher_type),
                        voucher_type=voucher_type,
                        date=from_tally_date(voucher_el.findtext("DATE") or ""),
                        party_name=voucher_el.findtext("PARTYLEDGERNAME") or "",
                        reference=voucher_el.findtext("REFERENCE") or "",
                        narration=voucher_el.findtext("NARRATION") or "",
                        remote_id=remote_id,
                        lines=lines,
                        bills=bills,
                        costs=costs,
                        stock=stock,
                    )
                    self.vouchers.append(voucher)
                    last_voucher_id = voucher.master_id
                    created += 1
                    continue
                existing.lines = lines
                existing.bills = bills
                existing.costs = costs
                existing.stock = stock
                existing.date = from_tally_date(voucher_el.findtext("DATE") or "")
                existing.narration = voucher_el.findtext("NARRATION") or ""
                existing.reference = voucher_el.findtext("REFERENCE") or ""
                last_voucher_id = existing.master_id
                altered += 1
                continue

            when = from_tally_date(voucher_el.findtext("DATE") or "")
            if self.edu and when is not None and when.day not in (1, 2, 31):
                errors.append(
                    "Educational version of Tally allows entry of vouchers only "
                    "on the 1st, 2nd and 31st of a month"
                )
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
                remote_id=remote_id,
                voucher_number=number,
                voucher_type=voucher_type,
                date=from_tally_date(voucher_el.findtext("DATE") or ""),
                party_name=voucher_el.findtext("PARTYLEDGERNAME") or "",
                reference=voucher_el.findtext("REFERENCE") or "",
                narration=voucher_el.findtext("NARRATION") or "",
                lines=lines,
                bills=bills,
                costs=costs,
                stock=stock,
            )
            self.vouchers.append(voucher)
            last_voucher_id = voucher.master_id
            created += 1

        return self._import_response(
            created, altered, errors, last_voucher_id, last_master_id
        )

    def _handle_company_import(
        self, root: etree._Element, elements: list[etree._Element]
    ) -> bytes:
        """Company creation over XML.

        TallyPrime 1.1.7.1 refuses this - every import resolves a current
        company first - so the refusal is reproduced verbatim when
        ``supports_company_create`` is off, which is what sends the bootstrap
        tool down its Tier 3 path in tests.
        """
        if not self.supports_company_create:
            return self._import_response(
                0, 0, ["Could not find Company ''"], "", ""
            )
        created = 0
        errors: list[str] = []
        for element in elements:
            name = element.findtext("NAME") or element.get("NAME") or ""
            if not name:
                errors.append("Company name is empty")
                continue
            if name in self.companies:
                errors.append(f"Company '{name}' already exists")
                continue
            self.companies.append(name)
            self.company = name
            created += 1
        return self._import_response(created, 0, errors, "", str(next(self._ids)))

    def create_company_via_ui(self, name: str) -> None:
        """What the Tier 3 keyboard path achieves, without a keyboard.

        The fake cannot be driven by keystrokes, so this is how a test asserts
        the outcome of the UI path: the company exists and is loaded.
        """
        if name not in self.companies:
            self.companies.append(name)
        self.company = name

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


def _quantity(raw: object) -> Decimal:
    """``20 Nos`` -> 20."""
    text = str(raw or "").strip()
    if not text:
        return Decimal("0")
    try:
        return Decimal(text.split()[0].replace(",", ""))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _rate(raw: object) -> Decimal:
    """``2500.00/Nos`` -> 2500.00."""
    text = str(raw or "").strip()
    if not text:
        return Decimal("0")
    try:
        return Decimal(text.split("/")[0].replace(",", "").strip())
    except Exception:  # noqa: BLE001
        return Decimal("0")


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
