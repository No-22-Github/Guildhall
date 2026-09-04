"""guildhall-server 入口:uvicorn + lifespan(autostart sandbox-agent)。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn

from . import api
from .api import AppContext
from .config import load as load_config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = load_config()

    @asynccontextmanager
    async def lifespan(app):
        context = AppContext(config)
        await context.manager.ensure_running()
        await context.manager.assert_agent_available()
        api.ctx = context
        app.state.ctx = context
        yield
        await context.shutdown()

    app = api.build_app(AppContext(config))
    app.router.lifespan_context = lifespan
    uvicorn.run(app, host="127.0.0.1", port=config.server_port, log_level="info")


if __name__ == "__main__":
    main()
