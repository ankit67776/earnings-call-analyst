"""Review the extraction golden set in the browser.

Serves one page on localhost that shows each passage with its labeled quotes marked.
Accept, edit or reject each drafted claim, or select text in a paragraph to add a claim
the draft missed. Every change is saved back to golden/extraction.json.

    uv run python -m evals.review [--port 8765]
"""

import argparse
import json
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from evals.build_golden import GOLDEN

PAGE = Path(__file__).with_name("review.html")

TYPES = ("risk", "guidance", "metric", "event")
STATUSES = ("agreed", "adjudicated", "draft", "accepted", "edited", "added", "rejected")


def check(data: dict) -> list[str]:
    """Problems that would make the golden set unusable; empty when it's fine."""
    problems = []
    for passage in data["passages"]:
        texts = {p["id"]: p["text"] for p in passage["paragraphs"]}
        for i, claim in enumerate(passage["claims"], 1):
            where = f"{passage['id']} claim {i}"
            text = texts.get(claim.get("paragraph_id"))
            if text is None:
                problems.append(f"{where}: unknown paragraph")
                continue
            if not claim.get("quote") or claim["quote"] not in text:
                problems.append(f"{where}: quote isn't in the paragraph")
            for alt in claim.get("alt_quotes", []):
                alt_text = texts.get(alt.get("paragraph_id"), "")
                if not alt.get("quote") or alt["quote"] not in alt_text:
                    problems.append(f"{where}: an alternative quote isn't in its paragraph")
            if claim.get("type") not in TYPES:
                problems.append(f"{where}: unknown type")
            if claim.get("status") not in STATUSES:
                problems.append(f"{where}: unknown status")
            if claim.get("status") != "rejected" and not claim.get("claim", "").strip():
                problems.append(f"{where}: claim text is empty")
    return problems


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/":
            self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/golden":
            self._send(HTTPStatus.OK, GOLDEN.read_bytes(), "application/json")
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    def do_PUT(self) -> None:
        if self.path != "/api/golden":
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            return
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Only claims change here; passages always come from the file.
        current = json.loads(GOLDEN.read_text("utf-8"))
        claims = {p["id"]: p["claims"] for p in data["passages"]}
        for passage in current["passages"]:
            passage["claims"] = claims.get(passage["id"], passage["claims"])
        problems = check(current)
        if problems:
            body = json.dumps({"problems": problems}).encode()
            self._send(HTTPStatus.UNPROCESSABLE_ENTITY, body, "application/json")
            return
        text = json.dumps(current, indent=2, ensure_ascii=False) + "\n"
        GOLDEN.write_text(text, encoding="utf-8")
        self._send(HTTPStatus.OK, b"{}", "application/json")

    def _send(self, status: HTTPStatus, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m evals.review")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Reviewing {GOLDEN} at {url} (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
