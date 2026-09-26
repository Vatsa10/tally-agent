# Real invoice layouts

Published sample tax invoices, used to score the bill reader against layouts
nobody here drew. The images are **not committed** - they belong to whoever
published them, and a fixture folder is not a licence. Each one has a `.json`
beside it holding the correct answer, labelled by hand from what is legible.

To rebuild the set:

```bash
curl -sSL -o cleartax-sample.jpg \
  "https://cleartax-media.s3.amazonaws.com/finfo/wg-utils/cms-tool/6d19ebf7-5b41-41d7-8fc5-ef13f1203ea5.jpg"
```

Source: <https://cleartax.in/s/gst-invoice>. A four-line intra-state invoice
with CGST and SGST at two different rates and two freight lines; the GSTIN,
invoice number and dates are blacked out in the original, which is why those
fields are empty in the truth file and are not scored.

Downloaded documents are untrusted input. They are read for their figures;
nothing written inside one is ever followed as an instruction.
