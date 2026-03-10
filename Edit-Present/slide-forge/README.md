# Slide Forge

A deployable app wrapper for the slide pipeline in `gemini_pipeline/`.

This project is intended to let lab users upload a PDF, convert each PDF page into PNG, run the existing slide SVG pipeline on every page, preview the generated SVG pages, and download the final PPTX.

## Recommended architecture

Do not run the long PDF -> PNG -> SVG -> PPTX pipeline inside Vercel serverless functions.

Recommended setup:
- Frontend: deploy `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-web` to Vercel
- Backend: deploy `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-api` on your own persistent Linux server
- Core pipeline: keep using the existing Python workflow under `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/gemini_pipeline`

This split is intentional:
- frontend is static and Vercel-friendly
- backend is long-running, file-heavy, and depends on Python runtime + local job workspace

## What the app does

1. User uploads one PDF
2. Backend renders each PDF page to PNG
3. Backend runs `gemini_pipeline/gemini_svg_pipeline.py --profile slide`
4. Backend stores the uploaded PDF, rendered PNG pages, generated SVG pages, and final PPTX in a job workspace
5. Frontend polls job status, previews SVG pages, and exposes the PPTX download

## Repository layout

- `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-api`
  - FastAPI backend service
- `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-web`
  - static frontend for Vercel
- `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/gemini_pipeline`
  - existing slide pipeline and compiler
- `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/gemini_pipeline/app_data/jobs`
  - runtime job workspace used by the backend
- `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/deploy/systemd`
  - systemd example service file
- `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/deploy/nginx`
  - nginx reverse proxy example

## Before pushing to GitHub

Do this before you push the project to GitHub:

1. Check whether `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/gemini_pipeline/config/runtime_api_config.json` still contains real API keys
2. If it does, do not push it to a public repository unchanged
3. Keep the repository private, or replace real keys with server-side values before publishing

At minimum, treat the runtime config as a server secret.

## Backend deployment on your server

Assumptions:
- server OS: Ubuntu 22.04 or similar
- domain: `slides-api.yourlab.example`
- deployment path: `/opt/slide-forge`
- Python version: `3.10+`

### 1. Clone the repository

```bash
cd /opt
git clone <YOUR_GITHUB_REPO_URL> slide-forge
cd /opt/slide-forge
```

### 2. Create virtualenv and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r apps/slide-api/requirements.txt
```

### 3. Prepare runtime config

The backend calls the existing slide pipeline, which reads:
- `/opt/slide-forge/gemini_pipeline/config/runtime_api_config.json`

Make sure that file contains the server-side API settings you actually want to use.

### 4. Smoke test the backend manually

```bash
cd /opt/slide-forge
source .venv/bin/activate
uvicorn main:app --app-dir apps/slide-api --host 0.0.0.0 --port 8000
```

Then from another shell:

```bash
curl http://127.0.0.1:8000/api/health
```

Expected: JSON with `ok: true`.

### 5. Run backend with systemd

Copy and edit the example file:
- `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/deploy/systemd/slide-api.service`

Install it on the server:

```bash
sudo cp deploy/systemd/slide-api.service /etc/systemd/system/slide-api.service
sudo systemctl daemon-reload
sudo systemctl enable slide-api
sudo systemctl start slide-api
sudo systemctl status slide-api
```

Check logs:

```bash
journalctl -u slide-api -f
```

### 6. Put nginx in front of it

Copy and edit the example file:
- `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/deploy/nginx/slide-api.conf`

Install it on the server:

```bash
sudo cp deploy/nginx/slide-api.conf /etc/nginx/sites-available/slide-api.conf
sudo ln -s /etc/nginx/sites-available/slide-api.conf /etc/nginx/sites-enabled/slide-api.conf
sudo nginx -t
sudo systemctl reload nginx
```

Then add TLS using your normal certbot / acme flow.

## Frontend deployment to Vercel

Frontend source:
- `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-web`

### 1. Set backend API base

Edit:
- `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-web/config.js`

Set:

```js
window.SLIDE_APP_CONFIG = {
  apiBase: "https://slides-api.yourlab.example"
};
```

### 2. Deploy

```bash
cd /Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-web
vercel
```

You can also import this folder directly into Vercel from GitHub.

## User flow after deployment

1. Open the Vercel frontend URL
2. Upload one PDF and choose the image placeholder strategy (`source-crop`, `gemini`, `auto`, or `off`)
3. Wait for the backend job to render PNGs and run the slide pipeline
4. Inspect the generated SVG pages in the browser
5. Download the final PPTX

## Job data and cleanup

Runtime job files are written under:
- `/opt/slide-forge/gemini_pipeline/app_data/jobs`

Current backend behavior:
- one job gets one job directory
- job log is stored in that directory
- uploaded source PDF is stored under `source/`
- rendered PNG pages are stored under `input/<deckStem>/`
- generated SVG pages are stored under `output/svg/<deckStem>/`
- generated PPTX is stored under `output/pptx/`
- API job status also exposes the source PDF URL and per-page PNG/SVG artifact URLs
- data remains on the server until explicitly removed

If you expect many users, add a scheduled cleanup job later.

## Known deployment limits

Current backend design is single-node and filesystem-backed:
- job state is stored on local disk
- long-running work is done in-process
- best deployed as one persistent backend instance first

Do not start with multi-instance autoscaling until job state and queueing are redesigned.

## Local development

### Backend

```bash
cd /Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber
python -m venv .venv
source .venv/bin/activate
pip install -r apps/slide-api/requirements.txt
uvicorn main:app --app-dir apps/slide-api --host 0.0.0.0 --port 8000 --reload
```

### Frontend

Because `apps/slide-web` is static, you can serve it with any simple static server.

Example:

```bash
cd /Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-web
python3 -m http.server 3000
```

Then point `config.js` to your local backend.

## Related docs

- `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/README.md`
- `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-api/README.md`
- `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-web/README.md`
- `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/gemini_pipeline/COMPILER_TECH_REPORT.md`
