# slide-api

Python backend for the slide pipeline app.

## What it does
- Accept a PDF upload
- Render each page to PNG
- Run the existing `gemini_pipeline/gemini_svg_pipeline.py` slide workflow
- Persist the uploaded PDF, rendered PNG pages, generated SVG pages, and final PPTX inside a per-job workspace on the server
- Expose job status, source PDF URL, per-page PNG/SVG preview URLs, and the final PPTX download
- Accept a per-job `refill_mode` option so users can choose source crop, model generation, auto fallback, or disable refill

## Run locally

```bash
cd /Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber
python -m venv .venv
source .venv/bin/activate
pip install -r apps/slide-api/requirements.txt
uvicorn apps.slide-api.main:app --host 0.0.0.0 --port 8000 --reload
```

If your shell dislikes the dash in the module path, use:

```bash
uvicorn main:app --app-dir apps/slide-api --host 0.0.0.0 --port 8000 --reload
```

## Important environment variables
- `SLIDE_APP_CORS_ORIGINS`: comma-separated frontend origins, default `*`
- `SLIDE_APP_MAX_PDF_MB`: upload cap in MB, default `64`
- `SLIDE_APP_PDF_RENDER_SCALE`: PDF page render scale, default `2.0`
- `SLIDE_APP_MAX_CONCURRENT`: pipeline page concurrency, default `8`

## Upload form parameters
- `file`: uploaded PDF
- `refill_mode`: one of `source-crop`, `gemini`, `auto`, `off`

## Notes
- This backend is intended for a persistent Python host.
- The frontend can be deployed on Vercel, but the long-running PDF->PNG->SVG->PPTX worker should not be run inside Vercel serverless functions.
- Provide the real API key through environment variables on the server, for example `OPENAI_API_KEY`.
- Runtime job artifacts are kept under `gemini_pipeline/app_data/jobs/<jobId>/` until explicitly deleted.
