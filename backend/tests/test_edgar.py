import json

import httpx2
import pytest

from app.filings.edgar import EdgarClient, EdgarError, load_ref
from tests.fakes import FakeTime

pytestmark = pytest.mark.anyio

UA = "Jane Doe jane@example.com"
TICKERS = {"0": {"cik_str": 1451809, "ticker": "SITM", "title": "SiTime Corp"}}


def columns(*filings: dict) -> dict:
    """EDGAR's column-wise filings format."""
    return {key: [f[key] for f in filings] for key in filings[0]}


def filing(form: str, period: str, accession: str, doc: str = "doc.htm") -> dict:
    return {
        "form": form,
        "reportDate": period,
        "filingDate": "2021-02-16",
        "accessionNumber": accession,
        "primaryDocument": doc,
    }


def make_client(routes: dict[str, httpx2.Response], seen: list | None = None) -> EdgarClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        return routes.get(str(request.url), httpx2.Response(404))

    time = FakeTime()
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return EdgarClient(UA, http=http, sleep=time.sleep, clock=time.clock)


SUBMISSIONS = {
    "name": "SITIME Corp",
    "filings": {
        "recent": columns(
            filing("10-K/A", "2020-12-31", "0001-21-000009"),
            filing("10-K", "2020-12-31", "0001-21-000005", "sitm-10k_20201231.htm"),
            filing("10-Q", "2020-09-30", "0001-20-000040"),
        ),
        "files": [{"name": "CIK0001451809-submissions-001.json"}],
    },
}
OLDER = columns(filing("10-K", "2015-12-31", "0001-16-000001", "old.htm"))
BASE = "https://www.sec.gov"
DATA = "https://data.sec.gov/submissions"
ROUTES = {
    f"{BASE}/files/company_tickers.json": httpx2.Response(200, json=TICKERS),
    f"{DATA}/CIK0001451809.json": httpx2.Response(200, json=SUBMISSIONS),
    f"{DATA}/CIK0001451809-submissions-001.json": httpx2.Response(200, json=OLDER),
}


async def test_finds_the_original_10k_for_a_fiscal_year() -> None:
    seen: list[httpx2.Request] = []
    ref = await make_client(ROUTES, seen).find_annual_report("sitm", 2020)
    assert (ref.ticker, ref.company, ref.accession) == ("SITM", "SITIME Corp", "0001-21-000005")
    assert ref.url == f"{BASE}/Archives/edgar/data/1451809/000121000005/sitm-10k_20201231.htm"
    assert ref.filename == "SITM_10-K_FY2020.htm"
    assert all(r.headers["user-agent"] == UA for r in seen)
    assert not any("submissions-001" in str(r.url) for r in seen)  # older page not needed


async def test_pages_through_older_filings() -> None:
    ref = await make_client(ROUTES).find_annual_report("SITM", 2015)
    assert ref.primary_document == "old.htm"


async def test_unknown_ticker_and_missing_year() -> None:
    client = make_client(ROUTES)
    with pytest.raises(EdgarError, match="unknown ticker"):
        await client.find_annual_report("NOPE", 2020)
    with pytest.raises(EdgarError, match="no 10-K"):
        await client.find_annual_report("SITM", 1999)


async def test_download_writes_document_and_metadata_once(tmp_path) -> None:
    seen: list[httpx2.Request] = []
    client = make_client(ROUTES, seen)
    ref = await client.find_annual_report("SITM", 2020)
    routes_with_doc = ROUTES | {ref.url: httpx2.Response(200, content=b"<html>10-K</html>")}
    client = make_client(routes_with_doc, seen)

    path = await client.download(ref, tmp_path)
    assert path.read_bytes() == b"<html>10-K</html>"
    assert load_ref(path) == ref
    assert json.loads(path.with_suffix(".json").read_text())["url"] == ref.url

    downloads = sum(str(r.url) == ref.url for r in seen)
    await client.download(ref, tmp_path)
    assert sum(str(r.url) == ref.url for r in seen) == downloads


async def test_retries_server_errors_then_gives_up() -> None:
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(503)

    time = FakeTime()
    client = EdgarClient(
        UA,
        http=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        sleep=time.sleep,
        clock=time.clock,
    )
    with pytest.raises(EdgarError, match="after retries"):
        await client.cik("SITM")
    assert len(calls) == 3


def test_requires_a_contact_user_agent() -> None:
    with pytest.raises(EdgarError, match="SEC_USER_AGENT"):
        EdgarClient("")
