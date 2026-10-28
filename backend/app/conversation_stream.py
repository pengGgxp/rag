"""Bridge the synchronous Agent to SSE without abandoning a billable in-flight call."""
import asyncio

import anyio


async def events(execution, stopped):
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    sentinel = object()

    def produce():
        try:
            for item in execution:
                loop.call_soon_threadsafe(queue.put_nowait, item)
        except BaseException as exc:
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, sentinel)

    task = asyncio.create_task(asyncio.to_thread(produce))
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=10)
            except TimeoutError:
                yield "heartbeat", {}
                continue
            if item is sentinel:
                break
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stopped.set()
        # Producer finishes the current HTTP call, records its usage, then observes stop.
        with anyio.CancelScope(shield=True):
            await asyncio.shield(task)
