import asyncio

_event: asyncio.Event | None = None


def sync_event() -> asyncio.Event:
    global _event
    if _event is None:
        _event = asyncio.Event()
    return _event


def request_sync():
    sync_event().set()
