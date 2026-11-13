#!/usr/bin/env python3
"""A <type> tag split across streamed tokens must never reach the speaker (2026-10-27)."""
import importlib.util, pathlib, sys
src = pathlib.Path(__file__).resolve().parents[3] / "gnome-speaks-service.py"
code = src.read_text()
# load only the pure router to avoid the service's GLib imports
ns = {}
start = code.index("def _route_type_tokens(")
end = code.index("\nclass ", start)
exec(code[start:end], ns)
route = ns["_route_type_tokens"]
def run(tokens):
    spoken, in_tag, carry = "", False, ""
    for t in tokens:
        s, in_tag, carry = route(t, in_tag, carry)
        spoken += s
    return spoken, carry
cases = [
    (["<", "type", ">Hello.", " How are you?", "</", "type>"], ""),      # split tags, all typed
    (["<type>", "Yes.", "</type>"], ""),                                   # whole tokens
    (["Sure, ", "<ty", "pe>ls -la</type>", " done."], "Sure,  done."),     # mixed
    (["plain", " text."], "plain text."),                                  # no tags
]
bad = 0
for toks, want in cases:
    got, carry = run(toks)
    ok = got == want and carry == ""
    print(("ok  " if ok else "FAIL"), toks, "->", repr(got))
    bad += not ok
sys.exit(1 if bad else 0)
