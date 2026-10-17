import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from app.llm.types import Completion, CompletionRequest

# Bump to invalidate every cached response, e.g. after changing how requests are built.
CACHE_VERSION = 1


def request_key(provider: str, request: CompletionRequest) -> str:
    payload = {"v": CACHE_VERSION, "provider": provider, **asdict(request)}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class DiskCache:
    """Response cache for development, so re-running a pipeline costs no quota.

    One JSON file per request, keyed by a hash of everything that affects the output.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, provider: str, request: CompletionRequest) -> Completion | None:
        path = self._path(request_key(provider, request))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Completion(**data["completion"], cached=True)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, provider: str, request: CompletionRequest, completion: Completion) -> None:
        path = self._path(request_key(provider, request))
        path.parent.mkdir(parents=True, exist_ok=True)
        stored = asdict(completion)
        del stored["cached"]
        data = {"request": asdict(request), "completion": stored}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def delete(self, provider: str, request: CompletionRequest) -> None:
        self._path(request_key(provider, request)).unlink(missing_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"
