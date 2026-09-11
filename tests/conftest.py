"""Shared fixtures. Every test talks to the fake Tally and never to a socket."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tallyagent_core.models import (
    Company,
    GSTDetails,
    Period,
    Voucher,
    VoucherLine,
    VoucherType,
)
from tallyagent_tally.backend import TallyBackend
from tallyagent_tally.client import TallyClient, TallyConfig
from tallyagent_tally.fake_server import FakeTally, seeded_demo

DEMO_COMPANY = "Demo Traders Pvt Ltd"
FY = Period(start=date(2026, 4, 1), end=date(2027, 3, 31), locked_before=date(2026, 4, 1))


@pytest.fixture
def fake_tally() -> FakeTally:
    return seeded_demo(DEMO_COMPANY)


@pytest.fixture
def tally_client(fake_tally: FakeTally) -> TallyClient:
    return TallyClient(
        TallyConfig(
            host="127.0.0.1",
            port=9000,
            company=DEMO_COMPANY,
            # The fake matches ids properly, so the alter path stays tested.
            supports_voucher_alter=True,
        ),
        transport=fake_tally.transport,
    )


@pytest.fixture
def backend(tally_client: TallyClient) -> TallyBackend:
    return TallyBackend(tally_client)


@pytest.fixture
def company() -> Company:
    return Company(
        name=DEMO_COMPANY, state_code="27", gstin="27AAPFU0939F1ZV", period=FY
    )


@pytest.fixture
def sales_voucher() -> Voucher:
    """Intra-state sale to Acme: 10,000 + 9% CGST + 9% SGST."""
    return Voucher(
        voucher_type=VoucherType.SALES,
        date=date(2026, 6, 15),
        party_name="Acme Industries",
        reference="INV-001",
        narration="Sale of goods",
        lines=[
            VoucherLine(ledger_name="Acme Industries", amount=Decimal("11800.00")),
            VoucherLine(ledger_name="Sales - GST 18%", amount=Decimal("-10000.00")),
            VoucherLine(ledger_name="Output CGST", amount=Decimal("-900.00")),
            VoucherLine(ledger_name="Output SGST", amount=Decimal("-900.00")),
        ],
        gst=GSTDetails(
            rate=Decimal("18"),
            taxable_value=Decimal("10000.00"),
            cgst=Decimal("900.00"),
            sgst=Decimal("900.00"),
            place_of_supply="27",
        ),
    )
