"""Read discovery/state and say a phrase using an explicitly selected bridge."""

import argparse

from nao_bridge_client import Nao


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:3000")
    args = parser.parse_args()
    with Nao(args.url) as nao:
        print(nao.robot.info().model_dump())
        print(nao.robot.state(wait_for_fresh_ms=1000).model_dump())
        print(nao.speech.say("Hello from NAO Bridge", timeout=10).data.model_dump())


if __name__ == "__main__":
    main()
