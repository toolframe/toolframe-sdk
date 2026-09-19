"""Synchronous page facade; the provider owns the asynchronous RPC event loop."""

import asyncio
import logging

from .contracts import PluginError


class PageContext:
    def __init__(self, runtime, resource, epoch, loop):
        self.owner = resource.owner
        self.generation = resource.generation
        self.log = logging.getLogger(f"toolframe.pages.{self.owner}")
        self.services = PageServices(runtime, resource.key, epoch, loop)


class PageServices:
    def __init__(self, runtime, key, epoch, loop):
        self.runtime, self.key, self.epoch, self.loop = runtime, key, epoch, loop

    def require(self, service, *, version=1):
        return PageService(self, service, version)


class PageService:
    def __init__(self, services, name, version):
        self.services, self.name, self.version = services, name, version

    def call(self, method, params=None, *, timeout=30):
        bridge = self.services
        try:
            if asyncio.get_running_loop() is bridge.loop:
                raise PluginError(
                    "page_thread_required",
                    "Synchronous pages must execute in a Streamlit script thread",
                )
        except RuntimeError:
            pass
        coroutine = bridge.runtime.resource_call(
            bridge.key,
            f"{self.name}@{self.version}/{method}",
            params,
            epoch=bridge.epoch,
            timeout=timeout,
        )
        future = asyncio.run_coroutine_threadsafe(coroutine, bridge.loop)
        try:
            return future.result(timeout=timeout + 1)
        except TimeoutError:
            future.cancel()
            raise PluginError(
                "deadline_exceeded", "Page service call exceeded its deadline"
            ) from None
