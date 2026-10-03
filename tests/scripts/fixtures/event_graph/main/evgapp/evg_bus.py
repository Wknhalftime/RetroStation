from collections.abc import Callable

_SUBSCRIBERS: dict[str, list[Callable[[dict[str, str]], None]]] = {}


def publish(topic: str, payload: dict[str, str]) -> None:
    for handler in _SUBSCRIBERS.get(topic, []):
        handler(payload)


def subscribe(topic: str, handler: Callable[[dict[str, str]], None]) -> None:
    _SUBSCRIBERS.setdefault(topic, []).append(handler)


def announce_batch(batch_id: str) -> None:
    publish("batch.finished", {"batch_id": batch_id})


def on_batch_finished(payload: dict[str, str]) -> None:
    print(payload["batch_id"])


def wire() -> None:
    subscribe("batch.finished", on_batch_finished)


def wire_inline() -> None:
    subscribe("batch.finished", lambda payload: print(payload["batch_id"]))
