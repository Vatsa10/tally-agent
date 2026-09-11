"""Stage 14.0: live-mode safety, EDU dates, encoding, and the richer probe.

Everything here is about the difference between the fake Tally (where a mistake
costs nothing) and a real installation (where it writes to somebody's books).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from tallyagent_core.livemode import DEFAULT_WRITE_PREFIX, LiveMode, WriteScopeError
from tallyagent_core.models import Company
from tallyagent_core.policy import Policy
from tallyagent_core.validation import EDU_ALLOWED_DAYS, ValidationContext, validate
from tallyagent_daemon import config as config_mod
from tallyagent_tally.client import TallyClient, TallyConfig
from tallyagent_tally.probe import ProbeReport, parse_counts, probe
from tallyagent_tally.xml import quirks
from tallyagent_tools import masters, vouchers
from tallyagent_tools.base import ToolContext

LIVE = LiveMode(enabled=True, write_prefix=DEFAULT_WRITE_PREFIX, edu=True)


# --- write scope ------------------------------------------------------------


def test_fake_mode_permits_every_company():
    fake = LiveMode()
    assert fake.may_write_to("Somebody Else Pvt Ltd")
    fake.require_write_scope("Somebody Else Pvt Ltd")
    assert fake.describe().startswith("fake Tally")


@pytest.mark.parametrize(
    ("company", "allowed"),
    [
        ("TA-Demo Traders", True),
        ("TA-", True),
        ("Demo Traders Pvt Ltd", False),
        ("ta-demo traders", False),  # case matters; a prefix check must be exact
        ("My TA-Company", False),  # prefix, not "contains"
        ("", False),
    ],
)
def test_live_mode_write_scope(company, allowed):
    assert LIVE.may_write_to(company) is allowed


def test_a_write_outside_the_scope_is_refused_with_advice():
    with pytest.raises(WriteScopeError, match="TA-") as caught:
        LIVE.require_write_scope("Real Client Pvt Ltd")
    message = str(caught.value)
    assert "Real Client Pvt Ltd" in message
    assert "protects books tallyagent did not create" in message


def test_an_empty_prefix_allows_nothing_rather_than_everything():
    """Fail closed: a misconfigured prefix must not become 'write anywhere'."""
    misconfigured = LiveMode(enabled=True, write_prefix="")
    assert not misconfigured.may_write_to("TA-Demo Traders")
    with pytest.raises(WriteScopeError, match="no company is writable"):
        misconfigured.require_write_scope("TA-Demo Traders")


def test_describe_surfaces_the_scope_and_edition():
    assert LIVE.describe() == "LIVE EDU - writes limited to TA-*"
    assert LiveMode(enabled=True, edu=False).describe() == "LIVE - writes limited to TA-*"


async def test_voucher_writes_are_refused_outside_the_scope(backend, fake_tally):
    """The guard sits on the single write path, ahead of validation."""
    ctx = ToolContext(
        backend=backend,
        company=Company(name="Real Client Pvt Ltd", state_code="27"),
        policy=Policy.default(),
        live=LIVE,
    )
    with pytest.raises(WriteScopeError):
        await vouchers.create_receipt(
            ctx, "Acme Industries", "100.00", voucher_date=date(2026, 6, 1)
        )
    assert fake_tally.vouchers == []
    # Nothing was even exported: the check happens before any Tally traffic.
    assert fake_tally.requests == []


async def test_master_writes_are_refused_outside_the_scope(backend, fake_tally):
    ctx = ToolContext(
        backend=backend,
        company=Company(name="Real Client Pvt Ltd", state_code="27"),
        policy=Policy.default(),
        live=LIVE,
    )
    with pytest.raises(WriteScopeError):
        await masters.create_ledger(ctx, "Rent", "Indirect Expenses")
    assert "Rent" not in fake_tally.ledgers


async def test_a_prefixed_company_writes_normally(backend, company, fake_tally):
    ctx = ToolContext(
        backend=backend,
        company=company.model_copy(update={"name": "TA-Demo Traders"}),
        policy=Policy.default(),
        live=LiveMode(enabled=True, edu=False),
    )
    result = await vouchers.create_receipt(
        ctx, "Acme Industries", "100.00", voucher_date=date(2026, 6, 15)
    )
    assert result.ok
    assert result.pending is not None


# --- EDU dates --------------------------------------------------------------


def test_core_and_adapter_agree_on_the_allowed_days():
    """core must not import the adapter, so the constant is duplicated. Pin it."""
    assert EDU_ALLOWED_DAYS == quirks.EDU_ALLOWED_DAYS == (1, 2, 31)


@pytest.mark.parametrize("day", [1, 2, 31])
def test_allowed_days_pass(day, company):
    voucher = _journal(date(2026, 7, day))
    report = validate(voucher, _ctx(company, edu=True))
    assert not [r for r in report.failures if r.rule == "edu_date_allowed"]


@pytest.mark.parametrize("day", [3, 15, 28, 30])
def test_other_days_are_blocked_in_edu_mode(day, company):
    voucher = _journal(date(2026, 7, day))
    failure = next(
        r for r in validate(voucher, _ctx(company, edu=True)).failures
        if r.rule == "edu_date_allowed"
    )
    assert "Educational mode" in failure.message
    assert failure.details["allowed_days"] == [1, 2, 31]


def test_the_rule_is_inert_on_a_licensed_install(company):
    report = validate(_journal(date(2026, 7, 15)), _ctx(company, edu=False))
    assert report.ok, report.summary()


def test_edu_allowed_dates_skips_a_31st_that_does_not_exist():
    assert quirks.edu_allowed_dates(2026, 2) == [date(2026, 2, 1), date(2026, 2, 2)]
    assert quirks.edu_allowed_dates(2026, 7)[-1] == date(2026, 7, 31)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (date(2026, 7, 1), date(2026, 7, 1)),      # already legal, untouched
        (date(2026, 7, 3), date(2026, 7, 2)),      # back to the 2nd
        (date(2026, 7, 29), date(2026, 7, 31)),    # forward to the 31st
        (date(2026, 2, 28), date(2026, 3, 1)),     # February has no 31st
        (date(2026, 12, 30), date(2026, 12, 31)),  # year end stays in the year
    ],
)
def test_snapping_picks_the_nearest_legal_date(given, expected):
    snapped, warning = quirks.snap_to_edu_date(given)
    assert snapped == expected
    assert bool(warning) is (snapped != given)


def test_snapping_never_moves_a_date_silently():
    _snapped, warning = quirks.snap_to_edu_date(date(2026, 7, 15))
    assert "not enterable" in warning
    assert "moved to 2026-07-31" in warning or "moved to 2026-07-02" in warning


def _journal(when: date):
    from tallyagent_core.models import Voucher, VoucherLine, VoucherType

    return Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=when,
        lines=[
            VoucherLine(ledger_name="Cash", amount=Decimal("100")),
            VoucherLine(ledger_name="Bank - HDFC 1234", amount=Decimal("-100")),
        ],
    )


def _ctx(company, edu: bool) -> ValidationContext:
    from tallyagent_core.models import Period

    return ValidationContext(
        company=company.model_copy(
            update={"period": Period(start=date(2026, 4, 1), end=date(2027, 3, 31))}
        ),
        known_ledgers={"Cash", "Bank - HDFC 1234"},
        edu_mode=edu,
    )


# --- encoding ---------------------------------------------------------------


def test_the_request_content_type_is_utf8():
    """Live Tally rejects every request when we declare UTF-16 and send UTF-8."""
    assert quirks.REQUEST_CONTENT_TYPE == "text/xml; charset=utf-8"


async def test_the_client_declares_the_charset_it_actually_sends():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["content-type"])
        request.content.decode("utf-8")  # must not raise
        return httpx.Response(200, content=b"<ENVELOPE/>")

    client = TallyClient(
        TallyConfig(host="127.0.0.1"), transport=httpx.MockTransport(handler)
    )
    await client.post(b"<ENVELOPE/>")
    assert seen == ["text/xml; charset=utf-8"]


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le"])
def test_a_utf16_response_is_decoded(encoding):
    body = (
        "<ENVELOPE><BODY><DATA><COLLECTION>"
        "<COMPANY><NAME>TA-X</NAME></COMPANY>"
        "</COLLECTION></DATA></BODY></ENVELOPE>"
    )
    from tallyagent_tally.xml import parsers

    assert parsers.parse_companies(body.encode(encoding)) == ["TA-X"]


def test_utf8_responses_are_untouched():
    assert not quirks.looks_utf16(b"<ENVELOPE><HEADER><STATUS>1</STATUS></HEADER>")
    assert quirks.clean_response(b"<A>x</A>") == b"<A>x</A>"


def test_utf16_detection_needs_more_than_one_stray_nul():
    """A UTF-8 body carrying a single control byte must not be mistaken."""
    body = b"<ENVELOPE><NAME>Acme\x00 Industries</NAME></ENVELOPE>"
    assert not quirks.looks_utf16(body)


# --- probe ------------------------------------------------------------------


def test_cmpinfo_counters_are_parsed():
    xml = (
        b"<ENVELOPE><BODY><DESC><CMPINFO>"
        b"<COMPANY>0</COMPANY><LEDGER>0</LEDGER><VOUCHER>0</VOUCHER>"
        b"</CMPINFO></DESC></BODY></ENVELOPE>"
    )
    assert parse_counts(xml) == {"COMPANY": 0, "LEDGER": 0, "VOUCHER": 0}
    assert parse_counts(b"<ENVELOPE/>") == {}


async def test_probe_against_the_fake_reports_ready(tally_client):
    report = await probe(tally_client, LiveMode())
    assert report.reachable
    assert report.companies_loaded == ["Demo Traders Pvt Ltd"]
    rendered = report.render()
    assert "PASS ready for live mode" in rendered
    assert "reachable" in rendered


async def test_probe_on_a_closed_port_fails_with_advice():
    client = TallyClient(TallyConfig(host="127.0.0.1", port=1, timeout_seconds=0.3))
    report = await probe(client, LiveMode())
    assert not report.reachable
    assert not report.ready
    assert "FAIL not ready for live mode" in report.render()
    assert any("cannot reach" in problem for problem in report.problems)


async def test_probe_flags_a_loaded_company_outside_the_write_scope(tally_client):
    report = await probe(tally_client, LIVE)
    assert any("NOT writable" in tip for tip in report.advice)
    assert "Demo Traders Pvt Ltd" in " ".join(report.advice)


def test_probe_report_renders_the_no_company_case():
    report = ProbeReport(url="http://127.0.0.1:9000", reachable=True)
    report.advice.append("no company is loaded - use /bootstrap to create one")
    rendered = report.render()
    assert "Select Company screen" in rendered
    assert "/bootstrap" in rendered


# --- config -----------------------------------------------------------------


def test_mode_defaults_to_fake_with_no_guard():
    config = config_mod.from_dict({})
    assert config.mode == "fake"
    assert not config.is_live
    assert config.live.may_write_to("anything at all")


def test_live_mode_turns_on_the_guard_and_edu_by_default():
    config = config_mod.from_dict({"mode": "live", "tally": {"host": "127.0.0.1"}})
    assert config.is_live
    assert config.live.write_prefix == "TA-"
    assert config.live.edu, "a student install is the common case for live mode"
    assert not config.live.may_write_to("Real Client Pvt Ltd")


def test_edu_can_be_turned_off_for_a_licensed_install():
    config = config_mod.from_dict(
        {"mode": "live", "tally": {"host": "127.0.0.1", "edu": False}}
    )
    assert config.is_live
    assert not config.live.edu


def test_the_write_prefix_is_configurable():
    config = config_mod.from_dict(
        {"mode": "live", "tally": {"write_prefix": "SANDBOX-"}}
    )
    assert config.live.may_write_to("SANDBOX-Client")
    assert not config.live.may_write_to("TA-Demo Traders")


def test_the_example_config_ships_in_fake_mode():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    config = config_mod.load(root / "config" / "config.example.toml")
    assert config.mode == "fake"
    assert not config.is_live
    assert config.tally_is_placeholder
