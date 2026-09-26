"""The client register: one practice, many sets of books.

What is being protected: a client's vouchers cannot end up in another client's
queue, and a switch moves the connection, the company and the database together
or not at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tallyagent_daemon import clients as clients_mod
from tallyagent_daemon import config as config_mod
from tallyagent_daemon.clients import Client, UnknownClientError, apply, load, parse

REGISTER = """
[storage]
data_dir = "data"

[[client]]
slug = "sharma"
name = "Sharma Textiles"
company = "Sharma Textiles"
bills_dir = "clients/sharma/bills"
bank_ledger = "Bank - HDFC 4411"

[[client]]
slug = "gupta"
name = "Gupta Transport"
company = "Gupta Transport Pvt Ltd"
host = "192.168.1.40"
port = 9002
state_code = "24"
"""


def test_a_register_reads_both_local_and_remote_clients():
    register = parse(_toml(REGISTER))

    assert register.slugs() == ["sharma", "gupta"]
    assert not register.get("sharma").is_remote
    assert register.get("gupta").is_remote, "a client's own machine over a VPN"
    assert register.get("gupta").port == 9002


def test_a_table_style_register_works_too():
    """``[client.sharma]`` reads more naturally when somebody hand-edits it."""
    register = parse(
        _toml(
            """
            [client.sharma]
            company = "Sharma Textiles"
            """
        )
    )

    assert register.get("sharma").company == "Sharma Textiles"


def test_each_client_gets_its_own_database():
    register = parse(_toml(REGISTER))

    first = register.get("sharma").database(register.data_dir)
    second = register.get("gupta").database(register.data_dir)

    assert first != second
    assert Path(first).name == "sharma.db"


def test_an_unknown_client_says_which_ones_exist():
    register = parse(_toml(REGISTER))

    with pytest.raises(UnknownClientError, match="sharma, gupta"):
        register.get("nobody")


def test_a_client_with_no_company_is_refused():
    """The company name is what Tally knows the books by; without it every read
    would silently run against whatever is loaded."""
    with pytest.raises(ValueError, match="no company name"):
        parse(_toml('[[client]]\nslug = "x1"\n'))


def test_a_slug_that_cannot_be_a_filename_is_refused():
    for bad in ("Sharma Textiles", "../etc", "a", "x" * 50):
        with pytest.raises(ValueError, match="not a usable client id"):
            parse(_toml(f'[[client]]\nslug = "{bad}"\ncompany = "X"\n'))


def test_the_same_client_listed_twice_is_refused():
    with pytest.raises(ValueError, match="listed twice"):
        parse(
            _toml(
                '[[client]]\nslug = "sharma"\ncompany = "A"\n'
                '[[client]]\nslug = "sharma"\ncompany = "B"\n'
            )
        )


def test_a_single_client_install_has_no_register_and_that_is_fine(tmp_path):
    register = load(tmp_path / "nothing.toml")

    assert register.empty


# --- switching ---------------------------------------------------------------


def test_switching_moves_the_connection_the_company_and_the_database_together():
    """Any one of the three left behind is how one client's voucher lands in
    another client's approval queue."""
    config = config_mod.from_dict(
        {
            "mode": "live",
            "tally": {"host": "127.0.0.1", "port": 9000, "company": "TA-Demo Traders"},
        }
    )
    client = Client(
        slug="gupta",
        name="Gupta",
        company="Gupta Transport Pvt Ltd",
        host="192.168.1.40",
        port=9002,
        state_code="24",
    )

    switched = apply(config, client, data_dir="data")

    assert switched.tally.host == "192.168.1.40"
    assert switched.tally.port == 9002
    assert switched.tally.company == "Gupta Transport Pvt Ltd"
    assert switched.company.name == "Gupta Transport Pvt Ltd"
    assert switched.company.state_code == "24", "GST depends on which state"
    assert switched.db_path.endswith("gupta.db")


def test_switching_leaves_the_original_config_alone():
    """The register is read once at startup and shared; a switch that mutated it
    would change which books the other window is working on."""
    config = config_mod.from_dict({"tally": {"company": "TA-Demo Traders"}})

    apply(config, Client(slug="gupta", name="G", company="Gupta"), data_dir="data")

    assert config.company.name == "TA-Demo Traders"


def test_the_financial_year_is_the_practice_wide_one():
    """A firm closes every client's April to March at once; the period is not a
    per-client fact and duplicating it would be a thing to get out of step."""
    config = config_mod.from_dict(
        {
            "tally": {"company": "TA-Demo Traders"},
            "company": {"fy_start": "2026-04-01", "fy_end": "2027-03-31"},
        }
    )

    switched = apply(config, Client(slug="g", name="G", company="Gupta"))

    assert switched.company.period == config.company.period


def _toml(text: str) -> dict:
    import tomllib

    return tomllib.loads(text)


def test_a_client_reads_as_a_line_in_a_list():
    line = Client(
        slug="gupta", name="Gupta Transport", company="Gupta Transport Pvt Ltd",
        host="192.168.1.40",
    ).describe()

    assert "gupta" in line and "192.168.1.40:9000" in line


def test_the_module_offers_what_the_cli_needs():
    assert hasattr(clients_mod, "load")
    assert hasattr(clients_mod, "apply")


def test_a_slug_typed_in_capitals_is_the_same_client():
    """It gets typed at a prompt; case is not a distinction worth making."""
    register = parse(_toml('[[client]]\nslug = "Sharma"\ncompany = "Sharma Textiles"\n'))

    assert register.slugs() == ["sharma"]
    assert register.get("SHARMA").company == "Sharma Textiles"
