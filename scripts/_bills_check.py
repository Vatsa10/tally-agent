import asyncio, logging, time
from tallyagent_core import dotenv
from tallyagent_daemon import config as config_mod, wiring
from tallyagent_tools import bills

logging.basicConfig(level=logging.WARNING)


async def main():
    dotenv.load()
    wired = wiring.build(config_mod.load("config/config.toml", "config/policy.toml"))
    started = time.monotonic()
    result = await bills.ingest_folder(wired.services.tools, "demo/bills", limit=6, again=True)
    print(f"{time.monotonic() - started:.1f}s")
    for line in bills.as_table((result.data or {}).get("outcomes", [])):
        print(line)
    print(result.message)

asyncio.run(main())
