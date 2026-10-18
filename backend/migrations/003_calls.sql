-- Earnings calls and their transcripts.

create table calls (
    id           bigint generated always as identity primary key,
    ticker       text not null,
    company      text not null,
    fiscal_year  int  not null,
    quarter      int  not null check (quarter between 1 and 4),
    source       text not null unique,  -- where the audio came from, e.g. 'earnings21:4385072'
    duration     real not null,         -- seconds
    stt_provider text not null,
    stt_model    text not null,
    speakers     text,                  -- where speaker labels came from; null if there are none
    created_at   timestamptz not null default now()
);
create index calls_ticker_year on calls (ticker, fiscal_year, quarter);

-- Timed transcript segments. Consecutive segments by one speaker share a turn.
create table call_segments (
    call_id  bigint not null references calls (id) on delete cascade,
    position int    not null,  -- 1-based
    turn     int    not null,  -- 1-based
    speaker  text   not null,  -- '' when unknown
    start_s  real   not null,
    end_s    real   not null,
    text     text   not null,
    primary key (call_id, position)
);
