# Data

Raw files go in `data/raw/` (gitignored). Public sources only.

## 10-K filings
SEC EDGAR (https://www.sec.gov/edgar). Set `SEC_USER_AGENT` in `.env` ("Name email").

## Earnings-call audio + reference transcripts
Rev's Earnings-21 dataset: 44 calls from 2020 (~39h) with transcripts, speaker labels and entity tags.

Sparse checkout of metadata, reference transcripts, speaker RTTMs and only the audio we use (skips other vendors' ASR outputs and the other 41 calls):

```bash
cd data/raw
git clone --depth 1 --filter=blob:none --no-checkout https://github.com/revdotcom/speech-datasets.git
cd speech-datasets
git sparse-checkout init --no-cone
printf '%s\n' '/earnings21/*.md' '/earnings21/*.csv' '/earnings21/transcripts/' '/earnings21/rttms/' \
  '/earnings21/media/4385072.mp3' '/earnings21/media/4385939.mp3' '/earnings21/media/4382825.mp3' \
  > .git/info/sparse-checkout
git checkout HEAD
```

Transcripts are CC BY-SA 4.0 (`earnings21/LICENSE.md`). The audio has no stated license: use it locally for development and evals, don't redistribute it.

## Phase 0 set
Three Q3 2020 earnings calls from US 10-K filers with calendar fiscal years, one per sector, plus each company's FY2020 10-K (the fiscal year the call falls in).

| Company | Sector | Earnings-21 ID | Call | Audio | 10-K (FY2020) |
|---|---|---|---|---|---|
| SiTime (SITM) | Technology | 4385072 | Q3 2020, 43 min | 10 MB | [0001564590-21-005867](https://www.sec.gov/Archives/edgar/data/1451809/000156459021005867/sitm-10k_20201231.htm), filed 2021-02-16 |
| Hershey (HSY) | Consumer goods | 4385939 | Q3 2020 (Q&A only), 51 min | 12 MB | [0000047111-21-000007](https://www.sec.gov/Archives/edgar/data/47111/000004711121000007/hsy-20201231.htm), filed 2021-02-17 |
| ConocoPhillips (COP) | Energy | 4382825 | Q3 2020, 63 min | 45 MB | [0001562762-21-000027](https://www.sec.gov/Archives/edgar/data/1163165/000156276221000027/cop10k2020.htm), filed 2021-02-16 |

10-K primary documents are saved as `data/raw/10k/<TICKER>_10-K_FY2020.htm`.

Notes:
- Illumina (4375653) looks like a Q3 earnings call in the metadata but is the GRAIL acquisition call, so it's not used.
- The COP mp3 is over Groq Whisper's 25 MB upload limit; downsample to mono 16 kHz or chunk it before batch STT.
