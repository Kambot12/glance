"""End-to-end smoke test against a real server over a real WebSocket.

Launches main.py, parses the session URL from its console output, connects as a
browser would (Origin + token subprotocol), triggers a capture and asserts the
response arrives as a stream. Also checks that a wrong token is refused by the
live server, not just by the test client.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path

import websockets
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]
PORT = 8799
RECV_TIMEOUT = 20.0  # raised for a real model: first token waits on model load
URL_RE = re.compile(r"http://(127\.0\.0\.1|[\d.]+):(\d+)/#t=([A-Za-z0-9_-]+)")


async def read_until_url(proc, timeout=25.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    lines = []
    while loop.time() < deadline:
        line = await loop.run_in_executor(None, proc.stdout.readline)
        if not line:
            break
        lines.append(line)
        sys.stdout.write("  | " + line)
        m = URL_RE.search(line)
        if m:
            return m.group(0), m.group(3)
    raise TimeoutError("server never printed a session URL:\n" + "".join(lines))


async def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--processor", default="mock")
    ap.add_argument("--source", default="file:assets/sample.png")
    ap.add_argument("--timeout", type=float, default=RECV_TIMEOUT)
    ap.add_argument("--mode", default="summarize")
    args = ap.parse_args()
    recv_timeout = args.timeout

    cmd = [
        str(ROOT / ".venv/bin/python"), "-u", "main.py",
        "--source", args.source,
        "--processor", args.processor,
        "--no-hotkeys",
        "--port", str(PORT),
    ]
    print("$ " + " ".join(cmd[1:]))
    import os

    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1, env=env,
    )
    failures = []
    try:
        url, token = await read_until_url(proc)
        ws_url = f"ws://127.0.0.1:{PORT}/ws"
        origin = f"http://127.0.0.1:{PORT}"
        await asyncio.sleep(0.6)

        # 1. a browser-shaped connection with the right token streams a response
        async with connect(
            ws_url,
            subprotocols=["glance.v1", f"token.{token}"],
            additional_headers={"Origin": origin},
        ) as ws:
            await ws.send('{"type":"trigger","mode":"%s"}' % args.mode)
            chunks, events = [], []
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                import json

                msg = json.loads(raw)
                events.append(msg["type"])
                if msg["type"] == "chunk":
                    chunks.append(msg["text"])
                if msg["type"] == "end":
                    break
                if msg["type"] in ("error", "blocked"):
                    failures.append(f"server reported {msg['type']}: {msg.get('text')}")
                    break

            text = "".join(chunks)
            print(f"\n  events   : {events[0]} ... {events[-1]} ({len(events)} total)")
            print(f"  chunks   : {len(chunks)}")
            print(f"  response : {text.strip()[:400]}")
            if len(chunks) < 2:
                failures.append(f"expected a stream of chunks, got {len(chunks)}")
            if args.processor == "mock" and "mock:summarize" not in text:
                failures.append("response did not come from the mock processor")

            # 2. a follow-up reuses the last frame and carries history
            await ws.send('{"type":"followup","text":"what is the error rate?"}')
            follow = []
            while True:
                import json

                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=recv_timeout))
                if msg["type"] == "chunk":
                    follow.append(msg["text"])
                if msg["type"] == "end":
                    break
            joined = "".join(follow)
            print(f"  follow-up: {joined.strip()[:400]}")
            if args.processor == "mock" and "1 prior exchange" not in joined:
                failures.append("follow-up did not carry the previous exchange")
            if not joined.strip():
                failures.append("follow-up produced no output")

        # 3. the live server refuses a wrong token
        try:
            async with connect(
                ws_url,
                subprotocols=["glance.v1", "token.not-the-real-token"],
                additional_headers={"Origin": origin},
            ):
                failures.append("live server accepted a wrong token")
        except Exception as e:
            print(f"  bad token: refused ({type(e).__name__})")

        # 4. the live server refuses a foreign Origin
        try:
            async with connect(
                ws_url,
                subprotocols=["glance.v1", f"token.{token}"],
                additional_headers={"Origin": "http://evil.example"},
            ):
                failures.append("live server accepted a foreign Origin")
        except Exception as e:
            print(f"  bad origin: refused ({type(e).__name__})")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    label = "mock model" if args.processor == "mock" else f"{args.processor} model"
    print(f"PASS: {args.source} -> {label} -> websocket -> web view streams end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
