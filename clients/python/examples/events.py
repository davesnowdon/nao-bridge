"""Observe touch from two sessions; optionally speak on fresh touch edges."""

import argparse
import os
import threading

from nao_bridge_client import BridgeError, Nao


def print_observer(events):
    for event in events:
        print("observer:", event.type, event.data, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:3000")
    parser.add_argument("--speak-on-touch", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("NAO_BRIDGE_TOKEN")
    with (
        Nao(args.url, token=token, name="touch-application") as nao,
        Nao(args.url, token=token, name="independent-observer") as observer,
        nao.events.subscribe(
            events=["touch.changed"], samples=[{"groups": ["sensors"], "rate_hz": 5}]
        ) as events,
        observer.events.subscribe(events=["touch.changed"]) as observed,
    ):
        worker = threading.Thread(target=print_observer, args=(observed,), daemon=True)
        worker.start()
        try:
            for event in events:
                print("application:", event.type, event.data, flush=True)
                if args.speak_on_touch and event.type == "touch.changed" and event.data["pressed"]:
                    try:
                        nao.speech.say("Hello", wait=False)
                    except BridgeError as error:
                        print("Speech not admitted:", error.code, flush=True)
        finally:
            observed.close()
            worker.join(timeout=2)


if __name__ == "__main__":
    main()
