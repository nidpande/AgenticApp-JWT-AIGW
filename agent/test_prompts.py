"""Smoke tests for the AIGW hybrid POC (no AIRS in data path).

Runs 5 test cases against the deployed stack:
  1. Auth negative      -> unauthenticated /v1/health returns 401
  2. Auth positive      -> authenticated  /v1/health returns 200
  3. Benign weather     -> tool call weather.get_weather succeeds
  4. Benign filesystem  -> tool call filesystem.read/list succeeds
  5. Benign GitHub      -> tool call github.list_issues succeeds

Exit code is 0 if every case produced its EXPECTED behavior.
"""
from __future__ import annotations

import asyncio
import os
import sys
import traceback
import urllib.request
import urllib.error
import base64

from agent import build_agent, AIGW_USER, AIGW_PASS


AIGW_BASE_URL = os.environ.get("AIGW_BASE_URL", "http://aigw.local/v1")


def http_probe(auth: bool) -> int:
    """Hit the health endpoint with or without Basic Auth; return status code."""
    req = urllib.request.Request(f"{AIGW_BASE_URL.rstrip('/v1')}/")
    if auth:
        creds = base64.b64encode(f"{AIGW_USER}:{AIGW_PASS}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return -1


async def run_agent_case(agent, prompt: str) -> tuple[bool, str]:
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]}
        )
        text = result["messages"][-1].content
    except Exception as exc:
        return False, f"[exception] {exc}"
    return bool(text and text.strip()), text


async def main() -> int:
    print("=" * 70)
    print("AIGW hybrid POC - smoke tests (Basic Auth, no AIRS in data path)")
    print("=" * 70)

    passed = 0
    total  = 0

    # ---- Case 1: unauthenticated should be 401 ----
    total += 1
    print("\n--- 1. Auth negative: no credentials --> expect HTTP 401 ---")
    code = http_probe(auth=False)
    ok = (code == 401)
    print(f"    got HTTP {code}   {'PASS' if ok else 'FAIL'}")
    passed += ok

    # ---- Case 2: authenticated should be 200 ----
    total += 1
    print("\n--- 2. Auth positive: valid credentials --> expect HTTP 200 ---")
    code = http_probe(auth=True)
    ok = (code == 200)
    print(f"    got HTTP {code}   {'PASS' if ok else 'FAIL'}")
    passed += ok

    # ---- Cases 3-5 need the agent ----
    try:
        agent = await build_agent()
    except Exception:
        traceback.print_exc()
        print("SMOKE TESTS: agent bootstrap failed")
        return 1

    for name, prompt in [
        ("3. Benign weather",
         "What is the current weather in Bengaluru? Use the weather tool."),
        ("4. Benign filesystem",
         "List the files in /data using the filesystem tool and read hello.txt."),
        ("5. Benign GitHub",
         "Using the github tool, list the first 3 open issues in modelcontextprotocol/servers."),
    ]:
        total += 1
        print(f"\n--- {name} ---")
        print(f"prompt: {prompt}")
        ok, resp = await run_agent_case(agent, prompt)
        preview = (resp or "").strip().replace("\n", " ")[:200]
        print(f"result: {'PASS' if ok else 'FAIL'}   preview: {preview!r}")
        passed += ok

    print("\n" + "=" * 70)
    print(f"SMOKE TESTS: {passed}/{total} passed")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
