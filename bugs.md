## Bugs / correctness risks

1. No resource cleanup on errors (criba.py:253-313, :132-160, :189-195)
extract_pdf opens doc and per-page page/textpage handles but only closes them
on the happy path. If any page raises (corrupt object, extraction failure),
doc.close() / page.close() never run, leaking native PDFium handles. Same in
_extract_text_spans: tp = page.get_textpage() won't reach tp.close() (line
160) if anything in the loop raises. These need try/finally.

2. requirements.txt floor doesn't match the API in use
requirements.txt:1 pins pypdfium2>=4.0, but the code uses the v5-era helper
API (e.g. PdfImage(obj.raw, page=…), PdfTextObj(obj.raw, textpage=…),
get_objects(filter=…)). Installed is 5.9.0, which works. Allowing 4.x risks
constructor/method incompatibilities at install time. Pin to what's tested,
e.g. pypdfium2>=5,<6.

3. Image figure numbering gaps (criba.py:203-248)
fig_num += 1 happens before extraction is attempted (line 206), and failures
continue (lines 227, 239). A page where fig 1 fails produces output starting
at page_xxx_fig_002, with index: 2 and no fig 1: confusing for downstream
consumers. Increment/assign the index only after a successful write.

## Minor / robustness

4. Silent broad excepts (criba.py:106, 116, 120):  except Exception: pass in
metadata extraction hides real failures with no log line even under -v. At
least logger.debug(...) them.

5. No handling for encrypted/password-protected PDFs (criba.py:269) 
PdfDocument(str(pdf_path))raises a rawPdfiumErrorwith no friendly message; 
theif not pdf_path.exists()` check (line 260) is the only guard.

6. Date metadata not normalized: creationdate/moddate are emitted as raw PDF
strings (D:YYYYMMDD…). The README schema (line 47) lists them without noting
the format. Either normalize to ISO-8601 or document the raw format.

7. Same-line reordering with mixed font sizes (criba.py:166) — sorting by (y, 
x) on the top y means baseline-aligned text of different sizes can interleave
horizontally out of order. It's partially covered by the README's "Reading
order" caveat, but that caveat only mentions columns, not intra-line size
mixing.
