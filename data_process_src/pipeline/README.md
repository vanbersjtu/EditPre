# src overview (core I/O + responsibilities)

This folder contains the main pipeline scripts. Below is a concise map of each
script and its core functions, with expected inputs/outputs.

## pipeline.py
Purpose: Orchestrates end-to-end flow.

- main()
  - Input: `--input` directory containing original `.SVG` files
  - Output:
    - `<input>/output/placeholder/` (placeholder SVGs + mapping)
    - `<input>/output/semantic/` (semantic SVGs + meta)
    - `<input>/output/refill/` (refilled SVGs)
    - `<input>/output/final.pptx` (PPTX export)

## svg_image_placeholder.py
Purpose: Replace `<image>`/`<use>` with placeholders and (optionally) VLM captions.

- collect_unique_images(svg_files, extracted_dir, image_cache)
  - Input: SVG list, output dir
  - Output: unique image map for captioning
- generate_captions(...)
  - Input: image bytes + VLM config
  - Output: `caption_cache` (text) + `chart_cache` (bool)
- process_svg(svg_path, output_path, ...)
  - Input: original SVG
  - Output: placeholder SVG (same name), mapping entries
- main()
  - Input: `--input` dir of SVGs
  - Output:
    - placeholder SVGs in `--output`
    - `image_placeholders.json`
    - `extracted_images/` cache

## svg_image_generate.py
Purpose: Call image model to generate replacements for placeholders.

- build_generation_tasks(mapping, model)
  - Input: mapping from `image_placeholders.json`
  - Output: task list with prompt/size/alpha info
- call_image_with_retries(...)
  - Input: model + prompt + size
  - Output: bytes (PNG/JPEG) or None
- apply_alpha_mask(...) / remove_solid_background(...)
  - Input: generated image + original alpha data
  - Output: PNG bytes with preserved transparency
- main()
  - Input: placeholder mapping
  - Output:
    - `generated_images/`
    - `generated_manifest.json`
    - `generation_tasks.json`

## svg_image_apply.py
Purpose: Embed generated images back into placeholder SVGs.

- build_image_element(...)
  - Input: bbox + data URI
  - Output: `<image>` element
- load_manifest(...)
  - Input: `generated_manifest.json`
  - Output: mapping `{(svg_file, placeholder_id): image_file}`
- main()
  - Input: placeholder SVGs + manifest
  - Output: refilled SVGs in `--output`

## svg_text_semantic.py
Purpose: Semantic grouping of text into textbox/group hierarchy.

- extract_items_with_playwright(svg_path)
  - Input: SVG
  - Output: text items with bbox/ctm/style
- build_prompt(role_list)
  - Input: role set
  - Output: LLM prompt (JSON schema for tree plan)
- normalize_tree_plan(plan, item_ids)
  - Input: LLM plan
  - Output: sanitized `{nodes, root}` tree
- apply_plan_to_svg(tree, items, plan, ...)
  - Input: SVG tree + plan
  - Output: SVG with nested `<g data-type="textbox/text-group">`
- main()
  - Input: placeholder SVGs
  - Output:
    - semantic SVGs in `--output`
    - `meta/items/` (items JSON)
    - `meta/plans/` (normalized plan JSON)
    - `meta/raw/` (LLM raw responses)

## svg_to_pptx_slide.py
Purpose: Convert semantic/refilled SVGs to PPTX with layered rendering.

- collect_render_items(svg_path)
  - Input: SVG
  - Output: ordered list of vector layers + images (bbox + data)
- render_vector_layer(svg_path, elements, defs, out_png)
  - Input: SVG fragments
  - Output: PNG layer with transparency
- add_textbox(...)
  - Input: textbox bbox + text runs
  - Output: PPT textbox
- add_chart(...)
  - Input: chart spec (LLM-generated)
  - Output: PPT chart shape
- build_pptx(svg_paths, ...)
  - Input: SVG list (semantic/refill)
  - Output: multi-slide PPTX

## svg_image_refill.py (legacy)
Purpose: Single-script generate+apply (kept for compatibility).
Recommendation: Prefer `svg_image_generate.py` + `svg_image_apply.py`.
