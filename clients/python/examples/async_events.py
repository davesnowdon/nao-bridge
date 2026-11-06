"""Two asynchronous observers; gaps and snapshots never trigger speech."""

import argparse
import asyncio

from nao_bridge_client import AsyncNao, BridgeError


async def consume(nao, events, label, speak):
    async for event in events:
        print(label, event.type, event.data, flush=True)
        if speak and event.type == "touch.changed" and event.data["pressed"]:
            try:
                await nao.speech.say("Hello", wait=False)
            except BridgeError as error:
                print("Speech not admitted:", error.code, flush=True)


async def observe(url, speak):
    async with (
        AsyncNao(url, name="touch-application") as nao,
        AsyncNao(url, name="independent-observer") as observer,
        await nao.events.subscribe(
            events=["touch.changed"], samples=[{"groups": ["sensors"], "rate_hz": 5}]
        ) as events,
        await observer.events.subscribe(events=["touch.changed"]) as observed,
    ):
        workers = [
            asyncio.create_task(consume(nao, events, "application:", speak)),
            asyncio.create_task(consume(observer, observed, "observer:", False)),
        ]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:3000")
    parser.add_argument("--speak-on-touch", action="store_true")
    args = parser.parse_args()
    asyncio.run(observe(args.url, args.speak_on_touch))


if __name__ == "__main__":
    main()
