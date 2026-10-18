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
