import asyncio, logging, sys
from tallyagent_core import dotenv
from tallyagent_daemon import cli, config as config_mod, wiring

logging.basicConfig(level=logging.INFO, format="%(message)s")


async def main():
    dotenv.load()
    wired = wiring.build(config_mod.load("config/config.toml", "config/policy.toml"))
    cli._wire_fallback(wired)
    wired.services.tiers.config.fallback_enabled = True
    print("provider:", wired.services.router.name)
    result = await wired.services.agent().run(" ".join(sys.argv[1:]))
    print("---", result.text)

asyncio.run(main())
