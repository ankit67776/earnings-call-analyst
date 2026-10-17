from app.llm.types import CompletionRequest, Limits, ProviderResult


class FakeTime:
    """Manual clock; sleeping advances it instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeProvider:
    """Replays scripted results; an Exception in the script is raised instead."""

    name = "fake"

    def __init__(self, *script: ProviderResult | Exception, limits: Limits | None = None) -> None:
        self.script = list(script)
        self.requests: list[CompletionRequest] = []
        self._limits = limits or Limits(rpm=100, tpm=100_000, rpd=10_000, tpd=1_000_000)

    def limits(self, model: str) -> Limits:
        return self._limits

    async def complete(self, request: CompletionRequest) -> ProviderResult:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        pass


def result(text: str = "ok", *, finish_reason: str = "stop", tokens: int = 10) -> ProviderResult:
    return ProviderResult(
        text=text,
        finish_reason=finish_reason,
        model="fake-model",
        prompt_tokens=tokens,
        completion_tokens=tokens,
    )
