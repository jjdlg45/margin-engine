# Online Retail II - committed demo sample

The first 5,000 converted invoice lines of the UCI 'Online Retail II' dataset, with matching derived reference rows. Created by `scripts/download_online_retail.py --write-sample`.

- Source: Chen, D. (2012). Online Retail II [Dataset]. UCI Machine Learning Repository. https://doi.org/10.24432/C5CG6D
- License: CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). This sample is a format-converted excerpt; changes: two xlsx sheets converted to CSV, a deterministic ROW_ID and a constant CURRENCY=GBP column added, dates reduced to days.
- fx_rates.csv holds monthly GBP->USD month-end reference rates derived from ECB daily rates via frankfurter.dev (see docs/data-sources.md).
