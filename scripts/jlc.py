#!/usr/bin/env python3
"""Tiny CLI for the pcbparts MCP endpoint (JLCPCB stock/price lookups).
Usage: jlc.py <tool> '<json-arguments>' [--raw | --full]
       jlc.py --list                       list the tools the endpoint offers

  jlc.py jlc_get_part '{"lcsc":"C6186"}'
  jlc.py jlc_search '{"query":"100nF 0402 X7R"}'

--raw prints the reply as indented JSON (cut at 6000 characters), --full prints all of it.
"""
import json, re, sys, urllib.request

def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request("https://pcbparts.dev/mcp", data=body, headers={
        "content-type": "application/json", "accept": "application/json, text/event-stream"})
    text = urllib.request.urlopen(req, timeout=60).read().decode()
    d = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    if "error" in d:
        raise SystemExit(str(d["error"]))
    return d["result"]

def call(tool, args):
    d = {"result": rpc("tools/call", {"name": tool, "arguments": args})}
    out = "".join(c.get("text", "") for c in d["result"]["content"])
    try:
        return json.loads(out)
    except Exception:
        return out

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)
    if sys.argv[1] == "--list":
        for t in rpc("tools/list", {})["tools"]:
            print(f'{t["name"]:<22} {" ".join(t["description"].split())[:110]}')
        sys.exit(0)
    res = call(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else {})
    if "--full" in sys.argv:
        print(json.dumps(res))
    elif "--raw" in sys.argv or not isinstance(res, dict) or "results" not in res:
        print(json.dumps(res, indent=1)[:6000] if not isinstance(res, str) else res[:6000])
    else:
        for r in res["results"]:
            print(f'{r.get("lcsc"):<11} {str(r.get("model"))[:28]:<28} {str(r.get("manufacturer"))[:14]:<14} '
                  f'{str(r.get("package"))[:18]:<18} stk={r.get("stock"):<8} ${r.get("price")} '
                  f'{r.get("library_type")} {r.get("pcba_type","")} {r.get("mounting_type","")}')
        print("total:", res.get("total", len(res["results"])))
