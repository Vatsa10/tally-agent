"""Serve the fake TallyPrime over real HTTP.

Lets you run the whole system - daemon, web UI, WhatsApp - without TallyPrime
installed, by pointing [tally] host/port at this instead.

    uv run python scripts/dev_fake_tally.py
    # then set host = "127.0.0.1", port = 9000 in config/config.toml
"""

from __future__ import annotations

import argparse

from fastapi import FastAPI, Request, Response

from tallyagent_tally.fake_server import seeded_demo


def build_app(company: str = "Demo Traders Pvt Ltd") -> FastAPI:
    tally = seeded_demo(company)
    app = FastAPI(title="fake TallyPrime")
    app.state.tally = tally

    @app.get("/")
    async def banner() -> Response:
        # Real Tally answers a bare GET with a version banner; probe reads it.
        return Response(
            content=f"<html><body>TallyPrime Server {tally.version}</body></html>",
            media_type="text/html",
        )

    @app.post("/")
    async def envelope(request: Request) -> Response:
        body = await request.body()
        return Response(content=tally.handle(body), media_type="text/xml")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--company", default="Demo Traders Pvt Ltd")
    args = parser.parse_args()

    import uvicorn

    print(f"fake TallyPrime on http://{args.host}:{args.port} ({args.company})")
    uvicorn.run(build_app(args.company), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
