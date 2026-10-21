-- Contradiction finder: claims management makes on a call, each checked against the
-- 10-K passages that discuss the same thing.

create table call_claims (
    id             bigint generated always as identity primary key,
    call_id        bigint not null references calls (id) on delete cascade,
    position       int    not null,  -- 1-based, in order of first mention
    type           text   not null check (type in ('guidance', 'metric', 'risk', 'event')),
    claim          text   not null,
    speaker        text   not null,
    quote          text   not null,  -- the transcript's text, as found
    match          text   not null check (match in ('exact', 'normalized', 'elided', 'fuzzy')),
    at_s           real   not null,  -- seconds into the call where the quote starts
    model          text   not null,
    prompt_version int    not null,
    created_at     timestamptz not null default now(),
    unique (call_id, position)
);

-- One row per pair the verifier judged: a claim and one of its candidate 10-K passages.
create table claim_checks (
    claim_id       bigint not null references call_claims (id) on delete cascade,
    rank           int    not null,  -- 1-based, in the claim's retrieved candidates
    passage_id     bigint not null references passages (id) on delete cascade,
    -- null: the verifier returned no verdict for this passage.
    verdict        text   check (verdict in ('contradiction', 'consistent', 'unrelated')),
    reason         text,
    filing_quote   text,  -- as the model returned it
    -- Where the quote was found in the passage; null if it wasn't. A contradiction is
    -- only shown when its quote was found.
    match          text   check (match in ('exact', 'normalized', 'elided', 'fuzzy')),
    start_offset   int,
    end_offset     int,
    source_text    text,  -- the matched span, as filed
    paragraph_id   text,  -- the filing paragraph the span is in
    model          text   not null,
    prompt_version int    not null,
    created_at     timestamptz not null default now(),
    primary key (claim_id, rank)
);
