"""Synchronous page facade; the provider owns the asynchronous RPC event loop."""

import asyncio

from .contracts import PluginError


class PageContext:
    def __init__(
        self, runtime, resource, epoch, loop, *, access_context=None, access=None
    ):
        self.owner = resource.owner
        self.generation = resource.generation
        self.log = runtime.log.bind(
            **{
                "toolframe.page.owner": self.owner,
                "toolframe.page.generation": self.generation,
            }
        )
        self.services = PageServices(runtime, resource.key, epoch, loop, access_context)
        self.access = access or {}

    def can(self, scope):
        return scope in self.access.get("allowed_scopes", ())

    def auth(self, action, params=None):
        bridge = self.services
        return asyncio.run_coroutine_threadsafe(
            bridge.runtime.access(action, params, access_context=bridge.access_context),
            bridge.loop,
        ).result(timeout=30)


class PageServices:
    def __init__(self, runtime, key, epoch, loop, access_context=None):
        self.runtime, self.key, self.epoch, self.loop = runtime, key, epoch, loop
        self.access_context = access_context

    def require(self, service, *, version=1):
        return PageService(self, service, version)


class PageService:
    def __init__(self, services, name, version):
        self.services, self.name, self.version = services, name, version

    def call(self, method, params=None, *, timeout=30, activity=True):
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
            access_context=bridge.access_context,
            activity=activity,
        )
        future = asyncio.run_coroutine_threadsafe(coroutine, bridge.loop)
        try:
            return future.result(timeout=timeout + 1)
        except TimeoutError:
            future.cancel()
            raise PluginError(
                "deadline_exceeded", "Page service call exceeded its deadline"
            ) from None
