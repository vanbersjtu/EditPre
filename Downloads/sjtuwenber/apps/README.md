# Slide App Packaging

This repo now includes a deployable app wrapper around the slide pipeline.

## Layout
- `apps/slide-api`: Python backend service for PDF -> PNG -> SVG -> PPTX jobs
- `apps/slide-web`: static frontend intended for Vercel deployment

## Recommended deployment
- Deploy `apps/slide-web` to Vercel
- Deploy `apps/slide-api` to a persistent Python host

This split is intentional. The backend job is long-running, writes multiple files, and depends on the existing Python slide pipeline.
