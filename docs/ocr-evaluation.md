# Choosing the OCR backend

The spec (section 3A) asks for one OCR backend at a time, chosen from an evaluation on your own
documents rather than a leaderboard: GLM-OCR and PaddleOCR-VL score within a point of each other
overall, GLM-OCR leans ahead on tables and formulas, PaddleOCR-VL on raw text, scans and
photographed pages. `scripts/ocr_eval.py` runs that evaluation and writes the report the Phase 2
exit criteria require.

## 1. Build the evaluation set

One directory, for example `eval/ocr/`, kept out of git when the documents are confidential:

- Every document type and language you expect in production: invoices, contracts, receipts,
  statements, forms, slides; scanned, photographed and born-digital; each language you serve.
  Aim for 20 to 50 pages per type and language, including your hardest cases (old scans,
  handwriting, dense tables, stamps).
- Beside each document (PDF, PNG, JPEG or WebP), its ground truth as `<name>.gt.md`: the text a
  careful person would type, in reading order. Markdown syntax, spacing and line breaks are not
  scored, letters and their case are.
- An optional `manifest.json` that tags documents for the breakdowns:

  ```json
  {"invoice-001.pdf": {"language": "de", "type": "invoice"}}
  ```

## 2. Run both backends

Start the two OCR sidecars (each a docparse instance whose `DOCPARSE_VLM_BASE_URL` points at the
vLLM server for its model, see `sidecars/docparse`), then:

```bash
uv run python scripts/ocr_eval.py --docs eval/ocr \
  --backend glm_ocr=http://glm-ocr:8000/v1 --model glm_ocr=zai-org/GLM-OCR \
  --backend paddleocr_vl=http://paddleocr-vl:8080/v1 --model paddleocr_vl=PaddleOCR-VL-1.5-0.9B \
  --out docs/reports/ocr-eval-$(date +%F).md
```

Documents run one at a time per backend so the timings are comparable. Every page is read with
no confidence bar, so its text counts.

## 3. Read the report and decide

The report gives the character error rate (CER) and word error rate (WER), mean page confidence,
seconds per document and failures, overall and broken down by language and document type, and
names the backend with the lowest mean CER. Before following it, check the breakdowns: if the
other backend is clearly better on a type that matters most to you (tables, for example), weigh
that. Then:

1. Set `OCR_BACKEND` (and the matching `*_BASE_URL` and `*_MODEL`) in `.env`.
2. Pick `OCR_MIN_CONFIDENCE` from the confidence column: pages below it keep their original image
   for a vision model, so a bar just under the confidence of correctly read pages keeps the
   unreliable ones without discarding good text.
3. Restart the gateway. Artifacts made by the old backend stay cached under its name; documents
   are re-parsed with the new backend on demand.

Re-run the evaluation when a new model or model version is available, and keep each report in
`docs/reports/`.
