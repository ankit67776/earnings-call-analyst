"""Find and download annual reports from SEC EDGAR.

EDGAR asks for a descriptive User-Agent with a contact email and at most 10 requests
per second: https://www.sec.gov/os/accessing-edgar-data
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx2

SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"


class EdgarError(Exception):
    pass


@dataclass(frozen=True)
class FilingRef:
    ticker: str
    cik: int
    company: str
    form: str
    accession: str
    # Period the report covers, e.g. "2020-12-31".
    period: str
    filed: str
    primary_document: str

    @property
    def fiscal_year(self) -> int:
        return int(self.period[:4])

    @property
    def url(self) -> str:
        folder = self.accession.replace("-", "")
        return f"{SEC_WWW}/Archives/edgar/data/{self.cik}/{folder}/{self.primary_document}"

    @property
    def filename(self) -> str:
        return f"{self.ticker}_{self.form}_FY{self.fiscal_year}.htm"


class EdgarClient:
    def __init__(
        self,
        user_agent: str,
        *,
        http: httpx2.AsyncClient | None = None,
        min_interval: float = 0.15,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if "@" not in user_agent:
            raise EdgarError('SEC_USER_AGENT must be "Name email@example.com"')
        self._http = http or httpx2.AsyncClient(timeout=60.0, follow_redirects=True)
        self._headers = {"User-Agent": user_agent}
        self._min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last_request = float("-inf")
        self._lock = asyncio.Lock()
        self._ciks: dict[str, int] | None = None

    async def cik(self, ticker: str) -> int:
        if self._ciks is None:
            data = (await self._get(f"{SEC_WWW}/files/company_tickers.json")).json()
            self._ciks = {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}
        try:
            return self._ciks[ticker.upper()]
        except KeyError:
            raise EdgarError(f"unknown ticker {ticker!r}") from None

    async def find_annual_report(
        self, ticker: str, fiscal_year: int, form: str = "10-K"
    ) -> FilingRef:
        """The original (not amended) annual report whose period ends in `fiscal_year`."""
        cik = await self.cik(ticker)
        submissions = (await self._get(f"{SEC_DATA}/submissions/CIK{cik:010d}.json")).json()
        rows = _rows(submissions["filings"]["recent"])
        # Filers with long histories have older filings in extra pages; fetch only if needed.
        older = [f["name"] for f in submissions["filings"].get("files", [])]
        while True:
            for row in rows:
                if row["form"] == form and row["reportDate"].startswith(str(fiscal_year)):
                    return FilingRef(
                        ticker=ticker.upper(),
                        cik=cik,
                        company=submissions["name"],
                        form=form,
                        accession=row["accessionNumber"],
                        period=row["reportDate"],
                        filed=row["filingDate"],
                        primary_document=row["primaryDocument"],
                    )
            if not older:
                raise EdgarError(f"no {form} for {ticker} with a period ending in {fiscal_year}")
            rows = _rows((await self._get(f"{SEC_DATA}/submissions/{older.pop(0)}")).json())

    async def download(self, ref: FilingRef, dest_dir: Path) -> Path:
        """Save the primary document plus a JSON sidecar with the filing's metadata.
        Skips the request when the file is already there."""
        path = dest_dir / ref.filename
        if not path.exists():
            dest_dir.mkdir(parents=True, exist_ok=True)
            content = (await self._get(ref.url)).content
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(content)
            tmp.replace(path)
        path.with_suffix(".json").write_text(
            json.dumps(asdict(ref) | {"url": ref.url}, indent=1), encoding="utf-8"
        )
        return path

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str) -> httpx2.Response:
        for attempt in range(3):
            async with self._lock:
                wait = self._last_request + self._min_interval - self._clock()
                if wait > 0:
                    await self._sleep(wait)
                self._last_request = self._clock()
            try:
                response = await self._http.get(url, headers=self._headers)
            except (httpx2.TimeoutException, httpx2.TransportError) as e:
                error: Exception = e
            else:
                if response.status_code == 200:
                    return response
                if response.status_code not in (429, 500, 502, 503, 504):
                    raise EdgarError(f"GET {url}: HTTP {response.status_code}")
                error = EdgarError(f"GET {url}: HTTP {response.status_code}")
            await self._sleep(2.0**attempt)
        raise EdgarError(f"GET {url} failed after retries: {error}")


def load_ref(path: Path) -> FilingRef | None:
    """Metadata saved next to a downloaded filing, if any."""
    sidecar = path.with_suffix(".json")
    if not sidecar.exists():
        return None
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data.pop("url", None)
    return FilingRef(**data)


def _rows(page: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """EDGAR returns filings column-wise; turn them into one dict per filing."""
    return [dict(zip(page, values, strict=True)) for values in zip(*page.values(), strict=True)]
