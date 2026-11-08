# A tiny stand-in for an OpenAI-compatible server (POST /v1/chat/completions), for the watcher tests. Stdlib only.
# The model name picks the answer:
#   good        a verdict in a ```json block          garbled     plain words, no JSON
#   oddverdict  JSON with a verdict that is not one   nochoices   a reply with no choices in it
#   http500     HTTP 500                               noimage     HTTP 400 "does not support image input"
#   slow        waits 3 s, then good
# Run alone: python -I emulator/mock_openai.py [--port 8790]
import argparse, json, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LAST = {}  # the last request: path, headers, body


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        b = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the client gave up first (the timeout test)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        LAST.update(path=self.path, headers=dict(self.headers), body=body)
        if not self.path.endswith("/chat/completions"):
            return self.reply(404, {"error": {"message": "not found"}})
        model = body.get("model")
        if model == "http500":
            return self.reply(500, {"error": {"message": "server broke"}})
        if model == "noimage":
            return self.reply(400, {"error": {"message": "This model does not support image input."}})
        if model == "nochoices":
            return self.reply(200, {"object": "chat.completion"})
        if model == "slow":
            time.sleep(3)
        text = {"garbled": "Looks fine to me!",
                "oddverdict": '{"verdict": "maybe", "reason": "hard to say"}'}.get(
            model, '```json\n{"part": "seen", "verdict": "ok", "reason": "part on the plate"}\n```')
        return self.reply(200, {"object": "chat.completion", "model": model,
                                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}]})


def start(port=0):
    """Starts the mock in a thread; returns (server, base URL)."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    srv, url = start(ap.parse_args().port)
    print("mock OpenAI-compatible server at", url, flush=True)
    while True:
        time.sleep(3600)
