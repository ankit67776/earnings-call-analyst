-- Filings and their parsed text, claim-extraction runs, and the job queue.

create table documents (
    id          bigint generated always as identity primary key,
    ticker      text not null,
    company     text not null,
    form        text not null,
    fiscal_year int  not null,
    accession   text not null unique,
    period      date not null,
    filed       date not null,
    url         text not null,
    created_at  timestamptz not null default now()
);
create index documents_ticker_year on documents (ticker, fiscal_year);

create table sections (
    document_id bigint not null references documents (id) on delete cascade,
    item        text   not null,
    title       text   not null,
    position    int    not null,
    primary key (document_id, item)
);

create table paragraphs (
    document_id bigint not null,
    id          text   not null,  -- stable parser ID, e.g. '1A-3f9c2a1b'
    section     text   not null,
    position    int    not null,  -- 1-based within the section
    kind        text   not null check (kind in ('text', 'heading', 'table')),
    text        text   not null,
    primary key (document_id, id),
    foreign key (document_id, section) references sections (document_id, item) on delete cascade
);
create index paragraphs_in_order on paragraphs (document_id, section, position);

-- One run = one section analyzed with one model and prompt version.
create table analysis_runs (
    id                bigint generated always as identity primary key,
    document_id       bigint not null references documents (id) on delete cascade,
    section           text   not null,
    model             text   not null,
    prompt_version    int    not null,
    chunks            int    not null,
    llm_calls         int    not null,
    cached_calls      int    not null,
    prompt_tokens     int    not null,
    completion_tokens int    not null,
    seconds           real   not null,
    created_at        timestamptz not null default now()
);
create index analysis_runs_latest on analysis_runs (document_id, section, created_at desc);

-- What each LLM call saw and cost.
create table run_chunks (
    run_id            bigint  not null references analysis_runs (id) on delete cascade,
    position          int     not null,
    paragraph_ids     text[]  not null,
    context_heading   text,
    claims            int     not null,
    prompt_tokens     int     not null,
    completion_tokens int     not null,
    latency_ms        real    not null,
    cached            boolean not null,
    error             text,
    primary key (run_id, position)
);

create table findings (
    id       bigint generated always as identity primary key,
    run_id   bigint  not null references analysis_runs (id) on delete cascade,
    position int     not null,
    type     text    not null check (type in ('guidance', 'metric', 'risk', 'event')),
    claim    text    not null,
    -- false: the quote wasn't found in the source. Kept for evals, never shown as a finding.
    verified boolean not null
);
create index findings_by_run on findings (run_id, position);

create table citations (
    finding_id   bigint not null references findings (id) on delete cascade,
    position     int    not null,
    paragraph_id text   not null,
    quote        text   not null,  -- as the model returned it
    match        text   check (match in ('exact', 'normalized', 'elided', 'fuzzy')),
    start_offset int,
    end_offset   int,
    source_text  text,             -- the matched span, as filed
    primary key (finding_id, position)
);

-- Background work, claimed with FOR UPDATE SKIP LOCKED (no separate queue service).
create table jobs (
    id           bigint generated always as identity primary key,
    kind         text  not null,
    payload      jsonb not null default '{}',
    status       text  not null default 'queued'
                 check (status in ('queued', 'running', 'done', 'failed')),
    attempts     int   not null default 0,
    max_attempts int   not null default 3,
    run_after    timestamptz not null default now(),
    locked_by    text,
    locked_at    timestamptz,
    result       jsonb,
    error        text,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);
create index jobs_ready on jobs (run_after, id) where status = 'queued';
create index jobs_running on jobs (locked_at) where status = 'running';
