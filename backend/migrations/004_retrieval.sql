-- Retrieval over filings and call transcripts: passages with full-text search, plus
-- their embeddings from each embedding model being compared.

create extension if not exists vector;

create table passages (
    id            bigint generated always as identity primary key,
    ticker        text not null,
    document_id   bigint references documents (id) on delete cascade,
    call_id       bigint references calls (id) on delete cascade,
    position      int  not null,  -- 1-based, in source order
    title         text not null,  -- where it is, e.g. 'Item 7. MD&A › Results of Operations'
    text          text not null,
    -- Filings: the section and the paragraphs the passage spans.
    section       text,
    paragraph_ids text[],
    -- Calls: the segments, time span and speakers it covers.
    first_segment int,
    last_segment  int,
    start_s       real,
    end_s         real,
    speakers      text[],
    tsv           tsvector generated always as (
                      setweight(to_tsvector('english', title), 'B')
                      || setweight(to_tsvector('english', text), 'A')
                  ) stored,
    check ((document_id is null) <> (call_id is null))
);
create index passages_by_ticker on passages (ticker);
create index passages_fts on passages using gin (tsv);
create unique index passages_in_document on passages (document_id, position)
    where document_id is not null;
create unique index passages_in_call on passages (call_id, position) where call_id is not null;

-- One row per passage per model, e.g. 'voyage/voyage-4'. Searches always filter on one
-- model: vectors from different models aren't comparable. There's no ANN index: with a
-- few thousand passages per company an exact scan is fast and never misses a neighbour.
create table passage_embeddings (
    passage_id bigint       not null references passages (id) on delete cascade,
    model      text         not null,
    embedding  vector(1024) not null,
    primary key (model, passage_id)
);
