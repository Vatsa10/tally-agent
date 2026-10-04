"""A model that runs inside the firm, and the edit-log check on a client's books.

Both answer an objection the research found decisive: client data leaving the
office, and books an auditor would qualify.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from tallyagent_core.errors import NotConfiguredError
from tallyagent_llm.local import LocalProvider, is_private
from tallyagent_llm.provider import Message
from tallyagent_llm.router import ModelConfig, build_provider, stays_on_premises

# --- a local model -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:11434/v1", "http://localhost:1234/v1", "http://192.168.1.20:8000/v1",
     "http://10.0.0.5/v1", "http://[::1]:11434/v1"],
)
def test_this_machine_and_the_office_network_count_as_local(url):
    assert is_private(url)


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "http://8.8.8.8/v1", "nonsense"])
def test_anything_reachable_from_the_internet_does_not(url):
    assert not is_private(url)


def test_a_local_model_on_a_public_host_is_refused_at_start():
    """Otherwise the egress log would say nothing left the office when it did."""
    with pytest.raises(NotConfiguredError, match="not on this machine or a private network"):
        LocalProvider(base_url="http://8.8.8.8:11434/v1")


def test_a_local_model_needs_no_api_key():
    provider = build_provider(ModelConfig(provider="local", model=""))

    assert provider.name == "local"
    assert provider.base_url == "http://127.0.0.1:11434/v1"
    assert provider.model, "a sensible default model, not the cloud one's name"


def test_the_config_does_not_hand_a_local_model_the_cloud_models_name():
    from tallyagent_daemon import config as config_mod

    config = config_mod.from_dict({"model": {"provider": "local"}})

    assert config.model.model != "deepseek-flash"


async def test_a_local_model_answers_through_the_same_chat_api():
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        return httpx.Response(200, json={
            "model": "qwen2.5:14b-instruct",
            "choices": [{"message": {"role": "assistant", "content": "Trial balance agrees."}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        })

    provider = LocalProvider(transport=httpx.MockTransport(answer))
    completion = await provider.complete([Message(role="user", content="TB?")])

    assert completion.text == "Trial balance agrees."


def test_the_egress_log_can_say_what_stayed_in_the_office():
    assert stays_on_premises("http://127.0.0.1:11434/v1")
    assert stays_on_premises("local:mock")
    assert not stays_on_premises("https://api.deepseek.com")


# --- the edit log --------------------------------------------------------------


async def test_tally_reports_whether_the_edit_log_is_on():
    from tallyagent_tally.backend import TallyBackend
    from tallyagent_tally.client import TallyClient, TallyConfig
    from tallyagent_tally.fake_server import seeded_demo

    tally = seeded_demo("Sharma Textiles Pvt Ltd")
    backend = TallyBackend(
        TallyClient(TallyConfig(host="fake", company="Sharma Textiles Pvt Ltd"),
                    transport=tally.transport)
    )

    assert await backend.edit_log_on("Sharma Textiles Pvt Ltd") is False
    tally.edit_log.add("Sharma Textiles Pvt Ltd")
    assert await backend.edit_log_on("Sharma Textiles Pvt Ltd") is True
    assert await backend.edit_log_on("Nobody Ltd") is None, "unknown, not a guess"


@pytest.mark.parametrize(
    ("company", "flag", "needs"),
    [("Sharma Textiles Pvt Ltd", None, True), ("Gupta Exports Limited", None, True),
     ("Sharma & Sons", None, False), ("Mehta Traders", True, True),
     ("Acme Pvt. Ltd.", None, True), ("Big Ltd", False, False)],
)
def test_who_needs_an_edit_log(company, flag, needs):
    """Rule 3(1) applies to companies; nagging a proprietorship would bury the
    one warning that matters."""
    from tallyagent_daemon.clients import Client

    assert Client(slug="x1", name="x", company=company, companies_act=flag).needs_edit_log is needs


async def test_the_morning_run_flags_a_company_without_an_edit_log(tmp_path):
    from tallyagent_approvals.db import make_engine
    from tallyagent_daemon import clients as clients_mod
    from tallyagent_daemon import config as config_mod
    from tallyagent_daemon import wiring
    from tallyagent_daemon.firm.inbox import Inbox
    from tallyagent_daemon.firm.runner import FirmRunner
    from tallyagent_tally.fake_server import seeded_demo

    register = clients_mod.parse({
        "storage": {"data_dir": str(tmp_path / "data")},
        "client": [
            {"slug": "sharma", "company": "Sharma Textiles Pvt Ltd"},
            {"slug": "sons", "company": "Sharma & Sons"},
            {"slug": "logged", "company": "Logged Ltd"},
        ],
    })
    base = config_mod.from_dict({"storage": {"firm_db_path": str(tmp_path / "firm.db")}})
    tallies = {c.slug: seeded_demo(c.company) for c in register.clients}
    tallies["logged"].edit_log.add("Logged Ltd")

    def wire(client):  # type: ignore[no-untyped-def]
        return wiring.build(clients_mod.apply(base, client, register.data_dir),
                            transport=tallies[client.slug].transport)

    report = await FirmRunner(register, wire, Inbox(make_engine(str(tmp_path / "firm.db"))),
                              jobs=[]).run(date(2026, 7, 3))

    flagged = {o.client for o in report.outcomes if o.job == "compliance"}
    assert flagged == {"sharma"}, "not the partnership, not the company that has it on"
    line = next(o for o in report.outcomes if o.job == "compliance")
    assert "Rule 3(1)" in line.detail
