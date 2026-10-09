"""TokenHarbor free-chat parser -> OpenAI-compatible HTTP API.

Exposes TokenHarbor free web-chat models over /v1/chat/completions
(non-streaming and SSE streaming) and /v1/models.

Setup: copy config.example.json to config.json and fill it, then:
    python3 parser.py 31090
See README.md for how to get the values.
"""

import base64
import json
import os
import re
import sys
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 31090

SB_URL = "https://auth.tokenharbor.ai"
TH_URL = "https://tokenharbor.ai"

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("TH_CONFIG", os.path.join(HERE, "config.json"))
SESS_FILE = os.environ.get("TH_SESSION", os.path.join(HERE, "session.json"))


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        cfg = json.load(open(CONFIG_FILE))
    return {
        "anon_key": os.environ.get("TH_ANON_KEY", cfg.get("anon_key", "")),
        "refresh_token": os.environ.get("TH_REFRESH_TOKEN", cfg.get("refresh_token", "")),
        "sid": os.environ.get("TH_SID", cfg.get("sid", "")),
    }


CFG = load_config()
ANON_KEY = CFG["anon_key"]

STATIC_COOKIES = {
    "th_cookie_consent": "all",
    "th_geo": "1",
}
if CFG["sid"]:
    STATIC_COOKIES["th_sid"] = CFG["sid"]

MODELS = {
    "sim-base": "th-rudder:free",
    "deepseek-v4.1-flash": "deepseek-v4.1-flash:free",
    "mimo-v2.6-flash": "mimo-v2.6-flash:free",
    "claude-haiku-5.5": "claude-haiku-5.5:free",
}

sess = {}


def save():
    with open(SESS_FILE, "w") as f:
        json.dump(sess, f)


def load():
    global sess
    try:
        sess = json.load(open(SESS_FILE))
    except Exception:
        sess = {}
    if not sess.get("refresh_token") and CFG["refresh_token"]:
        sess["refresh_token"] = CFG["refresh_token"]


def chunk_cookie(b64):
    return "base64-" + b64[:3173], b64[3173:]


def refresh_session():
    req = urllib.request.Request(
        SB_URL + "/auth/v1/token?grant_type=refresh_token",
        data=json.dumps({"refresh_token": sess["refresh_token"]}).encode(),
        headers={"apikey": ANON_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    sess["access_token"] = d["access_token"]
    sess["refresh_token"] = d["refresh_token"]
    sess["expires_at"] = d["expires_at"]
    full = {
        "access_token": d["access_token"],
        "token_type": d.get("token_type", "bearer"),
        "expires_in": d.get("expires_in", 3600),
        "expires_at": d.get("expires_at"),
        "refresh_token": d["refresh_token"],
        "user": d.get("user", sess.get("user", {})),
    }
    b64 = base64.b64encode(json.dumps(full, separators=(",", ":")).encode()).decode()
    sess["sb0"], sess["sb1"] = chunk_cookie(b64)
    save()


def ensure_session():
    if not sess.get("refresh_token"):
        raise RuntimeError("no session: set TH_REFRESH_TOKEN")
    if sess.get("expires_at", 0) < time.time() + 120:
        refresh_session()


def cookie_header():
    parts = [f"{k}={v}" for k, v in STATIC_COOKIES.items()]
    parts.append(f"sb-auth-auth-token.0={sess['sb0']}")
    if sess.get("sb1"):
        parts.append(f"sb-auth-auth-token.1={sess['sb1']}")
    return "; ".join(parts)


def api(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        TH_URL + path, data=data, method=method,
        headers={"Cookie": cookie_header(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def stream_chat(upstream_model, prompt, timeout=120):
    ensure_session()
    s = api("POST", "/api/direct-chat/sessions", {"model": upstream_model})
    sid = s["session"]["id"]
    body = json.dumps({
        "sessionId": sid, "content": prompt, "model": upstream_model,
        "webSearch": "auto", "tz": "UTC",
    }).encode()
    req = urllib.request.Request(
        TH_URL + "/api/direct-chat/stream", data=body, method="POST",
        headers={"Cookie": cookie_header(), "Content-Type": "application/json",
                 "Accept": "text/event-stream"},
    )
    text, usage = [], {}
    with urllib.request.urlopen(req, timeout=timeout) as r:
        buf = b""
        while True:
            ch = r.read(1024)
            if not ch:
                break
            buf += ch
            while b"\n\n" in buf:
                evt, buf = buf.split(b"\n\n", 1)
                lines = evt.decode("utf-8", "replace").split("\n")
                kind = data = ""
                for ln in lines:
                    if ln.startswith("event:"):
                        kind = ln[6:].strip()
                    elif ln.startswith("data:"):
                        data += ln[5:].strip()
                if not data:
                    continue
                try:
                    d = json.loads(data)
                except Exception:
                    continue
                if kind == "chunk":
                    text.append(d.get("delta", ""))
                elif kind == "done":
                    usage = d.get("usage", {})
                elif kind == "error":
                    raise RuntimeError(d.get("message", "stream error"))
    return "".join(text), usage


def build_prompt(messages):
    parts = []
    for m in messages:
        role = m.get("role", "user")
        c = m.get("content", "")
        if isinstance(c, list):
            c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        if role == "system":
            parts.append(f"[System: {c}]")
        elif role == "assistant":
            parts.append(f"Assistant: {c}")
        elif role == "tool":
            parts.append(f"[Tool result: {c}]")
        else:
            parts.append(f"User: {c}")
    return "\n".join(parts).strip()


# ---------- homemade tools ----------

def build_tools_prompt(tools):
    defs = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        desc = fn.get("description") or "no description"
        schema = json.dumps(fn.get("parameters") or {"type": "object", "properties": {}})
        defs.append(f"- {name}: {desc}\n  parameters JSON schema: {schema}")
    if not defs:
        return ""
    return "\n".join([
        "[System: You have access to the following tools. When the user request needs one,",
        "call it by emitting EXACTLY one fenced block like this and nothing else outside it.",
        "The opening fence must be exactly ```toolcall (three backticks + the word toolcall).",
        "Bare JSON without the fence is NOT a valid call.",
        "",
        "```toolcall",
        '{"name": "<tool-name>", "arguments": {<args as JSON object>}}',
        "```",
        "",
        "Rules: output ONLY the fenced block when calling a tool (no prose before/after).",
        "If no tool is needed, answer normally in plain text without any fenced block.",
        "Available tools:",
        *defs,
    ])


def parse_tool_calls(text, tools):
    names = set()
    for t in tools or []:
        if isinstance(t, dict):
            n = (t.get("function") or {}).get("name")
            if n:
                names.add(n)
    found = []
    for m in re.finditer(r"```toolcall\s*([\s\S]*?)```", text or ""):
        try:
            obj = json.loads(m.group(1).strip())
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("name") in names:
            args = obj.get("arguments", {})
            found.append({
                "id": "call_" + uuid.uuid4().hex[:24],
                "type": "function",
                "function": {
                    "name": obj["name"],
                    "arguments": args if isinstance(args, str) else json.dumps(args),
                },
            })
    if not found:
        for m in re.finditer(
            r'\{\s*"name"\s*:\s*"([A-Za-z0-9_-]+)"\s*,\s*"arguments"\s*:\s*(\{[\s\S]*?\})\s*\}',
            text or "",
        ):
            if m.group(1) in names:
                try:
                    json.loads(m.group(2))
                except Exception:
                    continue
                found.append({
                    "id": "call_" + uuid.uuid4().hex[:24],
                    "type": "function",
                    "function": {"name": m.group(1), "arguments": m.group(2)},
                })
    return found


def strip_toolcall(text):
    t = re.sub(r"```toolcall\s*[\s\S]*?```", "", text or "")
    t = re.sub(r"^\s*toolcall\s*(\{[\s\S]*\})\s*$", "", t, flags=re.M)
    return t.strip()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, model, answer, tool_calls=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        cid = "chatcmpl-" + uuid.uuid4().hex
        now = int(time.time())
        if tool_calls:
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": now,
                     "model": model,
                     "choices": [{"index": 0,
                                  "delta": {"role": "assistant", "tool_calls": tool_calls},
                                  "finish_reason": None}]}
            self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
            done = {"id": cid, "object": "chat.completion.chunk", "created": now,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
            self.wfile.write(("data: " + json.dumps(done, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            try:
                self.wfile.flush()
            except Exception:
                pass
            return
        chunk = {"id": cid, "object": "chat.completion.chunk", "created": now,
                 "model": model,
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": answer},
                              "finish_reason": None}]}
        self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8"))
        done = {"id": cid, "object": "chat.completion.chunk", "created": now,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self.wfile.write(("data: " + json.dumps(done, ensure_ascii=False) + "\n\n").encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        try:
            self.wfile.flush()
        except Exception:
            pass

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            self._json(400, {"error": "bad json"})
            return
        model = data.get("model", "sim-base")
        upstream = MODELS.get(model)
        if not upstream:
            self._json(400, {"error": f"unknown model {model}, known: {sorted(MODELS)}"})
            return
        prompt = build_prompt(data.get("messages", []))
        tools = data.get("tools") if isinstance(data.get("tools"), list) else []
        want_tools = bool(tools) and data.get("tool_choice") != "none"
        if want_tools:
            tp = build_tools_prompt(tools)
            if tp:
                prompt = tp + "\n\n" + prompt
        if not prompt:
            self._json(400, {"error": "no prompt"})
            return
        t0 = time.time()
        try:
            answer, usage = stream_chat(upstream, prompt)
        except Exception as e:
            self._json(502, {"error": str(e)[:300]})
            return
        calls = parse_tool_calls(answer, tools) if want_tools else []
        if data.get("stream"):
            self._send_sse(model, answer, calls or None)
            return
        if calls:
            clean = strip_toolcall(answer)
            self._json(200, {
                "id": "chatcmpl-" + uuid.uuid4().hex,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": clean or None,
                                         "tool_calls": calls},
                             "finish_reason": "tool_calls"}],
                "usage": {
                    "prompt_tokens": usage.get("inputTokens", max(1, len(prompt) // 4)),
                    "completion_tokens": usage.get("outputTokens", max(1, len(answer) // 4)),
                    "total_tokens": usage.get("inputTokens", 0) + usage.get("outputTokens", 0) or max(1, (len(prompt) + len(answer)) // 4),
                },
                "_secs": round(time.time() - t0, 2),
            })
            return
        self._json(200, {
            "id": "chatcmpl-" + uuid.uuid4().hex,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                         "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": usage.get("inputTokens", max(1, len(prompt) // 4)),
                "completion_tokens": usage.get("outputTokens", max(1, len(answer) // 4)),
                "total_tokens": usage.get("inputTokens", 0) + usage.get("outputTokens", 0) or max(1, (len(prompt) + len(answer)) // 4),
            },
            "_secs": round(time.time() - t0, 2),
        })

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": int(time.time()), "owned_by": "tokenharbor"}
                for m in sorted(MODELS)]})
        else:
            self._json(200, {"ok": True, "service": "tokenharbor-parser"})


if __name__ == "__main__":
    if not ANON_KEY:
        print("FATAL: set TH_ANON_KEY or config.json anon_key", flush=True)
        sys.exit(1)
    load()
    if not sess.get("refresh_token"):
        print("FATAL: no session: set TH_REFRESH_TOKEN", flush=True)
        sys.exit(1)
    ensure_session()
    print(f"tokenharbor-parser on {HOST}:{PORT}, session exp {sess['expires_at']}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
