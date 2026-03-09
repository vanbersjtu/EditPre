# Pipeline File Map

Updated: 2026-03-10

## Active (required for current pipeline)

- `gemini_svg_pipeline.py`
- `svg_to_pptx_pro.py`
- `prompt.md`
- `qwen_image_2512_from_svg.py` (optional extension for placeholder image generation)
- `compiler/` (entire package)
- `config/`
  - `runtime_api_config.json` is committed without a live API key
  - `runtime_api_config.example.json` provides the publish-safe template
- `input/` (source PNGs)
- `output/` (generated SVG/PPTX)
- `app_data/jobs/` (runtime job workspace for the slide app backend)
- `COMPILER_TECH_REPORT.md`

## App Packaging (slide workflow)

- `apps/slide-api/`
  - FastAPI service wrapping `PDF -> PNG -> slide pipeline -> PPTX`
  - Persists uploaded PDF, rendered PNG pages, SVG output, PPTX output, and a temporary per-job `runtime_config.json` under `app_data/jobs/<jobId>/`
  - Exposes job status, source PDF URL, per-page PNG/SVG preview URLs, and final PPTX download
- `apps/slide-web/`
  - Static frontend for Vercel deployment
  - Uploads PDF, exposes request provider / model API base / code model / image model / refill strategy selection, polls backend status, previews generated SVGs, downloads PPTX
- `README.md`
  - root deployment and architecture guide for the app + pipeline
- `deploy/systemd/`
  - example systemd service for persistent backend hosting
- `deploy/nginx/`
  - example nginx reverse proxy config for the backend

## Archived to `past/20260305_pipeline_cleanup`

- `batch_convert_openai.py`
- `check_slide7_positions.py`
- `check_tspan_coords.py`
- `convert_single_image.py`
- `convert_single_image_openai.py`
- `convert_svg_to_pptx.py`
- `debug_coords.py`
- `debug_full_flow.py`
- `fix_svg_css.py`
- `generate_missing_svg.py`
- `print_tb_body.py`
- `regenerate_truncated_svg.py`
- `svg_to_pptx_exact_compiler.py`
- `test_chart_generation.py`
- `test_convert_slide7.py`
- `test_svg_parse.py`

## Notes

- This cleanup only reorganizes files under `gemini_pipeline/`.
- No files were deleted; archived files were moved.
- Main entrypoints were smoke-checked after cleanup:
  - `python gemini_pipeline/gemini_svg_pipeline.py --help`
  - `python gemini_pipeline/svg_to_pptx_pro.py --help`
