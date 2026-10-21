"""Read queries behind the API. Findings always come from the latest run of each section."""

from typing import Any

import asyncpg

# Latest run per section of one document.
_LATEST_RUNS = """
    select distinct on (section) *
    from analysis_runs
    where document_id = $1
    order by section, created_at desc, id desc
"""


async def list_documents(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        with latest as (
            select distinct on (document_id, section) id, document_id, section
            from analysis_runs
            order by document_id, section, created_at desc, id desc
        )
        select d.id, d.ticker, d.company, d.form, d.fiscal_year, d.filed,
               coalesce(array_agg(l.section order by l.section)
                        filter (where l.section is not null), '{}') as analyzed_sections,
               (select count(*) from findings f join latest l2 on f.run_id = l2.id
                where l2.document_id = d.id and f.verified) as findings,
               (select count(*) from findings f join latest l2 on f.run_id = l2.id
                where l2.document_id = d.id and not f.verified) as rejected
        from documents d
        left join latest l on l.document_id = d.id
        group by d.id
        order by d.ticker, d.fiscal_year desc
        """
    )
    return [dict(r) for r in rows]


async def get_document(conn: asyncpg.Connection, document_id: int) -> dict[str, Any] | None:
    doc = await conn.fetchrow(
        "select id, ticker, company, form, fiscal_year, accession, period, filed, url"
        " from documents where id = $1",
        document_id,
    )
    if doc is None:
        return None
    sections = await conn.fetch(
        """
        select s.item, s.title, count(p.*) as paragraphs
        from sections s left join paragraphs p
            on p.document_id = s.document_id and p.section = s.item
        where s.document_id = $1
        group by s.item, s.title, s.position
        order by s.position
        """,
        document_id,
    )
    runs = await conn.fetch(
        f"""
        with latest as ({_LATEST_RUNS})
        select l.id, l.section, l.model, l.prompt_version, l.chunks, l.llm_calls,
               l.prompt_tokens, l.completion_tokens, l.seconds, l.created_at,
               count(*) filter (where f.verified) as findings,
               count(*) filter (where not f.verified) as rejected
        from latest l left join findings f on f.run_id = l.id
        group by l.id, l.section, l.model, l.prompt_version, l.chunks, l.llm_calls,
                 l.prompt_tokens, l.completion_tokens, l.seconds, l.created_at
        order by l.section
        """,
        document_id,
    )
    return dict(doc) | {
        "sections": [dict(s) for s in sections],
        "runs": [dict(r) for r in runs],
    }


async def get_findings(
    conn: asyncpg.Connection, document_id: int, section: str | None = None
) -> list[dict[str, Any]]:
    """Findings with their citations; rejected claims (quote not found) are included
    with `verified` false so the UI can show what was dropped."""
    rows = await conn.fetch(
        f"""
        with latest as ({_LATEST_RUNS})
        select f.id, l.section, f.type, f.claim, f.verified,
               jsonb_agg(jsonb_build_object(
                   'paragraph_id', c.paragraph_id, 'match', c.match, 'quote', c.quote,
                   'start', c.start_offset, 'end', c.end_offset, 'source_text', c.source_text
               ) order by c.position) as citations
        from latest l
        join findings f on f.run_id = l.id
        join citations c on c.finding_id = f.id
        where $2::text is null or l.section = $2
        group by f.id, l.section, f.type, f.claim, f.verified, f.position
        order by l.section, f.position
        """,
        document_id,
        section,
    )
    return [dict(r) for r in rows]


async def get_section(
    conn: asyncpg.Connection, document_id: int, item: str
) -> dict[str, Any] | None:
    section = await conn.fetchrow(
        "select item, title from sections where document_id = $1 and item = $2",
        document_id,
        item,
    )
    if section is None:
        return None
    paragraphs = await conn.fetch(
        "select id, position, kind, text from paragraphs"
        " where document_id = $1 and section = $2 order by position",
        document_id,
        item,
    )
    return dict(section) | {"paragraphs": [dict(p) for p in paragraphs]}


async def list_companies(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    """Every company with a stored filing or call, and what the chat can search for it."""
    rows = await conn.fetch(
        """
        with tickers as (select ticker from documents union select ticker from calls)
        select t.ticker,
               coalesce((select company from documents d where d.ticker = t.ticker
                         order by fiscal_year desc limit 1),
                        (select company from calls c where c.ticker = t.ticker limit 1))
                   as company,
               coalesce((select jsonb_agg(jsonb_build_object(
                             'id', d.id, 'form', d.form, 'fiscal_year', d.fiscal_year)
                             order by d.fiscal_year desc)
                         from documents d where d.ticker = t.ticker), '[]') as documents,
               coalesce((select jsonb_agg(jsonb_build_object(
                             'id', c.id, 'fiscal_year', c.fiscal_year, 'quarter', c.quarter,
                             'duration', c.duration)
                             order by c.fiscal_year desc, c.quarter desc)
                         from calls c where c.ticker = t.ticker), '[]') as calls,
               (select count(*) from passages p where p.ticker = t.ticker) as passages
        from tickers t
        order by t.ticker
        """
    )
    return [dict(r) for r in rows]


async def get_call(conn: asyncpg.Connection, call_id: int) -> dict[str, Any] | None:
    call = await conn.fetchrow(
        "select id, ticker, company, fiscal_year, quarter, source, duration, stt_provider,"
        " stt_model, speakers as speaker_source from calls where id = $1",
        call_id,
    )
    if call is None:
        return None
    segments = await conn.fetch(
        "select position, turn, speaker, start_s as start, end_s as end, text"
        " from call_segments where call_id = $1 order by position",
        call_id,
    )
    return dict(call) | {"segments": [dict(s) for s in segments]}


async def get_call_checks(conn: asyncpg.Connection, call_id: int) -> dict[str, Any] | None:
    """A call's claims and how each fared against the 10-K. A pair is flagged only when
    the verifier called it a contradiction and its 10-K quote was found in the passage."""
    if not await conn.fetchval("select 1 from calls where id = $1", call_id):
        return None
    rows = await conn.fetch(
        """
        select cc.id, cc.position, cc.type, cc.claim, cc.speaker, cc.quote, cc.at_s as at,
               cc.model,
               coalesce(jsonb_agg(jsonb_build_object(
                   'rank', k.rank, 'verdict', k.verdict, 'reason', k.reason,
                   'flagged', k.verdict = 'contradiction' and k.match is not null,
                   'quote', k.source_text, 'match', k.match, 'passage_id', k.passage_id,
                   'title', p.title, 'section', p.section, 'document_id', p.document_id,
                   'paragraph_id', k.paragraph_id, 'model', k.model)
                   order by k.rank) filter (where k.claim_id is not null), '[]') as checks
        from call_claims cc
        left join claim_checks k on k.claim_id = cc.id
        left join passages p on p.id = k.passage_id
        where cc.call_id = $1
        group by cc.id
        order by cc.position
        """,
        call_id,
    )
    claims = [dict(r) for r in rows]
    checked = [c for c in claims if c["checks"]]
    return {
        "call_id": call_id,
        "claims": claims,
        "checked": len(checked),
        "flagged": sum(any(k["flagged"] for k in c["checks"]) for c in claims),
        "claims_model": claims[0]["model"] if claims else None,
        "verifier_model": checked[0]["checks"][0]["model"] if checked else None,
    }
