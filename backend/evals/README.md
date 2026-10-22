# Evals

## Claim extraction golden set

`golden/extraction.json` holds 18 passages from three FY2020 10-Ks (Hershey, SiTime,
ConocoPhillips): three from risk factors (Item 1A) and three from MD&A (Item 7) per
company. They were sampled with a fixed seed by `build_golden.py`. Each passage is a
run of whole paragraphs of about 2,500 characters, with the claims a careful analyst
would pull from it.

### How the labels were made

There was no domain expert to review the labels, so they come from two independent
annotators plus an adjudicator. All three are Claude, working from the rules below and
the filing text only, never from the extractor's output.

1. Two annotators labeled every passage separately (`golden/labelers/a.json`, 60
   claims, and `b.json`, 70 claims).
2. Their labels were compared with the same span matching the eval uses
   (`agreement.py`). They agreed on 59 claims: 91% positive agreement, and 97% type
   agreement on the claims both found.
3. A third pass settled the 14 disagreements without knowing which annotator proposed
   what. It kept 5 claims, retyped 2, dropped 3 and folded 4 into existing claims as
   alternative quotes. Every decision and its reason is in `golden/adjudication.json`.
4. `merge_labels.py` builds the 64 golden claims from those three files. Each claim's
   `status` is `agreed` (57) or `adjudicated` (7). A test checks that the committed
   golden set rebuilds exactly.

One disclosure: annotator A added the heading rule (below) after a first eval run
showed the models quoting risk-factor headings. The rule was applied to all 18
passages before annotator B labeled anything.

```bash
uv run python -m evals.build_golden   # re-sample passages (keeps existing claims)
uv run python -m evals.agreement golden/labelers/a.json golden/labelers/b.json
uv run python -m evals.merge_labels   # rebuild golden claims from labelers + adjudication
uv run python -m evals.review         # inspect or edit claims in the browser
```

### What counts as a claim

The same four types the extractor is asked for:

- **risk**: a specific risk or uncertainty the company says could hurt it. One claim per
  distinct risk. A list of risks gives one claim per item. A sentence that only restates
  the consequence of a risk already labeled ("...could negatively affect our operating
  results") is not a new claim.
- **guidance**: a forward-looking target, plan or expectation about the company's own
  results, spending, timing or exposure. This includes explicit negative expectations
  ("we do not expect any significant losses from counterparty defaults"). A description
  of strategy with no target, amount or date is not guidance.
- **metric**: a reported number about the company's results or its operating
  environment, such as benchmark commodity prices in MD&A. Dates alone are not metrics.
- **event**: a completed or announced corporate action: an acquisition, divestiture,
  offering, dividend or buyback change, restructuring or impairment.

Never a claim: forward-looking-statement disclaimers, generic boilerplate ("actual
results may differ from these estimates"), definitions, cross-references and lists of
applicable laws. A heading is never a claim on its own. When a risk-factor heading
states a labeled risk, it is listed as an alternative quote for that risk, so a model
that quotes the heading instead of the body still finds the claim.

### Quotes

A quote is the clause that states the claim, copied exactly from one paragraph, at most
about 40 words. Where a claim is stated in more than one place, `alt_quotes` lists the
other spans, each with its paragraph. A model quote that overlaps any of them counts as
the same claim.

### Scoring

A predicted claim matches a gold claim when one of its grounded quotes overlaps one of
the gold spans in the same paragraph, by at least half the shorter span. Pairs are
matched one to one, best overlap first, so a claim extracted twice counts once.

- **Precision**: the share of verified predicted claims that match a gold claim.
- **Recall**: the share of gold claims that were matched.
- **Type accuracy**: the share of matched pairs that agree on the claim type.
- **Grounding rate**: the share of the model's quotes that were found in the filing.
  Claims whose quote isn't found are dropped before scoring.
- **Citation accuracy**: the share of found quotes that sit in the paragraph the model
  cited.

## Speech-to-text

`wer.py` transcribes the three Earnings-21 calls (Hershey, SiTime, ConocoPhillips, Q3
2020) and scores them against Rev's reference transcripts, which are human-made and
verbatim: they keep fillers, false starts and repeated words.

- **WER**: substitutions, deletions and insertions over all reference words, after both
  sides go through the Whisper English normalizer (lowercase, no punctuation or fillers,
  numbers as digits, so "third quarter" and "3rd Quarter" match).
- **Number words wrong / name words wrong**: the share of reference words inside tagged
  entities that aren't transcribed exactly. Numbers are amounts, percentages, dates,
  ordinals and quantities; names are people, organizations and products. Each tagged
  entity is normalized as one piece, so "$32.7 million" stays one number.
- **Speaker accuracy**: speakers come from the dataset's reference diarization (RTTM),
  so this only checks the matching: the share of correctly transcribed words that land
  with the speaker the reference gives them.

The references live in `data/raw` (gitignored), so this eval runs locally, not in CI.

```bash
uv run python -m evals.wer --model whisper-large-v3 --model whisper-large-v3-turbo --readme
```

`live_stt.py` streams the same calls into Deepgram at 1x, start to end, and scores the
joined final results with the same WER and entity metrics. It runs two configurations:
numbers and dates written out (what live mode uses) and spoken. The gap between them is
how much of the WER is formatting.

- **Speakers right**: live mode has no reference diarization, so Deepgram's speaker
  labels are scored. Each label is matched to the reference speaker it shares the most
  correctly transcribed words with, biggest overlaps first and no speaker twice, and a
  word counts when its label's match is its speaker. A label that merges two people
  loses the smaller one's words; a person split over two labels loses one label's words.

All calls stream at once, so a run takes as long as the longest call (63 minutes).
Faster replay doesn't save time: Deepgram works through a stream at about 1.25x real
time, and audio sent faster only queues up. Runs are recorded in `.cache/live_stt`, so
re-scoring is free.

```bash
uv run python -m evals.live_stt --readme
```

## Retrieval golden set

`golden/retrieval.json` holds 120 questions about the three companies, each tied to the
passage that answers it. `build_retrieval.py` sampled 84 passages with a fixed seed, 14
per company and source (10-K or call), from passages of at least 60 words, skipping the
call's opening and safe-harbor statement and passages that are mostly table.

Two annotators (both Claude, working independently from the passage text only, never
from any retrieval output) went through the passages, one front to back and one back to
front, and wrote one question per passage until they had 10 per company and source:

- what an analyst would type into a research assistant, answerable from that passage
  alone and specifically (a number, reason, plan, named risk, comparison);
- paraphrased: no copied distinctive phrases, so keyword overlap doesn't decide it, but
  proper names and standard financial terms kept as an analyst would say them;
- with a short answer and the exact quote that states it.

`merge_retrieval.py` checks every quote is in its passage and builds the questions.

### Scoring

A retrieved passage is relevant when it contains the question's quote. Matching quotes
rather than passage IDs keeps the labels valid when passages are re-cut.

Other passages can answer a question too: a call repeats what the 10-K says, and an
answer can run on into the next passage. So the top 5 of every search system were pooled
(`evals.retrieval --pool 5`, about nine candidates a question) and four judges, again
Claude and blind to which system ranked what, marked the candidates that answer the
question with the reference answer's key facts on their own (`labelers/judgments_*.json`).
Their quotes are added to the question as `alt_quotes`. Passages ranked below 5 by every
system weren't judged, so recall@10 can still undercount.

- **Recall@k**: the share of questions with a relevant passage in the top k.
- **MRR@10**: the mean of 1 / rank of the first relevant passage (0 if none in the top 10).

```bash
uv run python -m evals.retrieval --readme   # needs the index (python -m app.retrieval)
```

## Chat golden set

`golden/chat.json` takes 30 answerable questions from the retrieval set (5 per company
and source, fixed seed), with their reference answers, and adds 24 unanswerable ones
from a third annotator (`labelers/unanswerable_c.json`). Those were written to sit close
to what the sources discuss, 8 per company: 3 details the sources talk around but never
state, 2 false premises, 1 period the sources can't cover, 1 about another company and
1 out of scope (a stock recommendation). Each was checked against the full text of
every passage the chat can search, with the nearest passages recorded.

Each question runs through the real pipeline: hybrid retrieval of 6 passages, the
answer prompt, quote verification.

- **Answered** / **correct refusals**: the share of answerable questions answered, and
  of unanswerable ones refused.
- **Correct**: of the answered, answerable questions, the share that give the reference
  answer's key facts, judged by an LLM.
- **Faithful sentences**: the share of answer sentences their cited quotes fully
  support, judged by an LLM that sees the sentence, its quotes and the cited passages.
- **Quotes found**: the share of the model's quotes located in the sources before
  verification drops the rest.
- **Context recall**: the share of answerable questions whose gold passage was among
  the 6 retrieved, which separates retrieval misses from answering misses.

The judge is `qwen/qwen3.8-27b`, a different model family from the gpt-oss models that
answer, so it isn't grading its own style. To see how far to trust it, a second labeler
(Claude, blind to the judge's verdicts, `labelers/chat_judge_d.json`) graded the same
answers: 85% agreement on sentence verdicts and 83% on correctness, with almost the same
overall share of fully supported sentences (83% against the judge's 81%). Live runs save each question's retrieved
passages (`cache/chat_contexts.json`) and every response, so CI replays the whole eval
offline.

```bash
uv run python -m evals.build_chat
uv run python -m evals.chat --readme
uv run python -m evals.chat --offline --min-answered 0.9 --min-refusal 0.9 --min-faithful 0.75
uv run python -m evals.judge_agreement score golden/labelers/chat_judge_d.json
```

## Contradiction golden set

`golden/contradictions.json` holds every claim the pipeline pulled from the three Q3
2020 calls (111, by `gpt-oss-20b`, after dropping 4 that analysts rather than executives
said), each with the three 10-K passages stage 1 retrieved for it: 333 claim-passage
pairs. The candidates are recorded, so the labels stay tied to exact pairs when passages
are re-cut or search is retuned.

### How the labels were made

Two annotators (Claude, working independently from the pair text only, before any
verifier had run) labeled all 333 pairs, one front to back and one back to front
(`labelers/contradictions_{a,b}.json`), with one of three labels:

- **contradiction**: the call quote and the passage are about the same thing (same
  measure, scope and period, or the same fact about the company) and can't both be
  true: figures that differ by more than rounding, opposite directions, or one saying
  something is so and the other that it isn't.
- **consistent**: the passage states the same thing, or something that supports it.
- **unrelated**: the passage doesn't speak to what the quote states.

Not contradictions: a forecast, target or plan against a different actual result or a
later, revised target; a risk the 10-K says could happen against a confident tone on
the call; figures for different periods or scopes (a quarter and the year, a segment
and the company, adjusted and reported); rounding and wording. The call quote is judged,
not the model's summary of it.

They agreed on 324 of 333 pairs (97%). Neither found a contradiction: on these calls,
management said nothing the 10-K contradicts. All 9 disagreements were consistent
against unrelated; a third reader settled them without knowing who said what
(`contradictions_adjudication.json`). Gold: 36 consistent, 297 unrelated.

### Seeded contradictions

With no real contradictions, recall can't be measured on real pairs. So a fourth
annotator wrote seeded ones (`labelers/contradictions_seeds.json`): 18 claims, 6 per
company, each a real claim rewritten as little as possible to contradict a statement in
one of its 10-K passages (a changed figure, a flipped direction, a negation, a date or a
counterparty), mostly from pairs both annotators called consistent. Stage 1 retrieves
candidates for the edited claim as it would for a real one, so a seed missed by
retrieval counts against recall too. Then two annotators labeled the seeded claims'
pairs blind, the same way as the real ones: a 10-K often states a fact in more than one
place (Bosch as the MEMS foundry is in both Item 1 and Item 1A), so a seed can
contradict several of its candidates, or none. Disclosure: at first only the passage a
seed was written against counted as contradicted and its other candidates as
unrelated. Early verifier runs flagged some of those other candidates, which on
inspection did contradict the seed, so the seeded pairs were labeled properly instead.
The annotators who did it never saw a verdict, but the decision to relabel came from
seeing results, and it raised measured precision.

### Scoring

A pair counts as flagged only as the app would show it: a contradiction verdict whose
10-K quote is found in the passage.

- **Precision**: the share of flagged pairs labeled contradiction (real and seeded).
- **Recall**: the share of pairs labeled contradiction that were flagged.
- **Seeded caught**: the share of seeded claims flagged against a passage they contradict.
- **False alarms**: the share of real claims flagged even though none of their pairs is
  a contradiction: how often an analyst would be sent chasing nothing.
- **Verdict agreement**: the share of real pairs whose verdict matches the label.
- **10-K quotes found**: the share of the verifier's 10-K quotes (for contradiction and
  consistent verdicts) located in the passage.

One disclosure: the "later, revised target" clause was added to the verifier prompt
after a 10-claim probe (COP-01 to COP-10) showed it flagging the call's $500 million
synergy target against the 10-K's raised $750 million one. The annotators' guide had the
same definitions throughout and they labeled that pair unrelated.

```bash
uv run python -m app.contradictions claims                 # extract claims from the calls
uv run python -m evals.build_contradictions claims         # record claims + candidates
uv run python -m evals.build_contradictions check evals/golden/labelers/contradictions_a.json
uv run python -m evals.build_contradictions agreement
uv run python -m evals.build_contradictions seeds          # add seeded claims (needs the DB)
uv run python -m evals.build_contradictions merge
uv run python -m evals.contradictions --model openai/gpt-oss-120b --model openai/gpt-oss-20b --readme
```
