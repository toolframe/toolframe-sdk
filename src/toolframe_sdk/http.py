"""Optional ASGI lifetime helper. HTTP libraries are provider dependencies."""

import asyncio
import socket

from .contracts import PluginError


class HttpServer:
    def __init__(self, app):
        self.app = app
        self.server = None
        self.task = None
        self.socket = None
        self.url = None

    async def start(self):
        import uvicorn

        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.socket.setblocking(False)
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                log_level="warning",
                access_log=False,
                lifespan="on",
                timeout_graceful_shutdown=5,
            )
        )
        self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        try:
            async with asyncio.timeout(20):
                while not self.server.started:
                    if self.task.done():
                        await self.task
                        raise PluginError(
                            "http_start_failed", "Provider HTTP listener did not start"
                        )
                    await asyncio.sleep(0.01)
        except BaseException:
            await self.close()
            raise
        return self.url

    async def close(self):
        if self.server:
            self.server.should_exit = True
        try:
            if self.task:
                async with asyncio.timeout(8):
                    await asyncio.shield(self.task)
        finally:
            if self.socket:
                self.socket.close()
