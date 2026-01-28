#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Run placeholder -> semantic -> generate -> apply -> pptx in sequence.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def run_cmd(cmd, cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True)


def validate_generation_outputs(generate_dir: Path) -> None:
    manifest_path = generate_dir / "generated_manifest.json"
    tasks_path = generate_dir / "generation_tasks.json"
    if not manifest_path.exists():
        raise SystemExit(f"Missing generated manifest: {manifest_path}")
    if not tasks_path.exists():
        raise SystemExit(f"Missing generation tasks: {tasks_path}")
    with manifest_path.open("r", encoding="utf-8") as f:
        manifest = json.load(f)
    with tasks_path.open("r", encoding="utf-8") as f:
        tasks = json.load(f)
    if len(manifest) != len(tasks):
        raise SystemExit(f"Generated images incomplete: {len(manifest)}/{len(tasks)}")
    images_dir = generate_dir / "generated_images"
    missing = []
    for item in manifest:
        image_file = item.get("image_file")
        if not image_file:
            continue
        if not (images_dir / image_file).exists():
            missing.append(image_file)
    if missing:
        sample = ", ".join(missing[:5])
        raise SystemExit(f"Generated images missing ({len(missing)}): {sample}")


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Run placeholder/semantic/generate/apply/pptx pipeline.")
    parser.add_argument("--input", required=True, help="Input directory with original SVGs.")
    parser.add_argument("--dataset", default="dataset", help="Output dataset directory.")
    parser.add_argument("--config", default="config.json", help="Config json path.")
    parser.add_argument("--skip-placeholder", action="store_true", help="Skip placeholder generation.")
    parser.add_argument("--skip-semantic", action="store_true", help="Skip text semantic grouping.")
    parser.add_argument("--skip-generate", action="store_true", help="Skip image generation.")
    parser.add_argument("--skip-apply", action="store_true", help="Skip image apply step.")
    parser.add_argument("--skip-pptx", action="store_true", help="Skip PPTX export.")
    args = parser.parse_args()

    input_dir = Path(args.input).expanduser().resolve()
    dataset_arg = Path(args.dataset).expanduser()
    dataset_dir = (input_dir / dataset_arg).resolve() if not dataset_arg.is_absolute() else dataset_arg.resolve()
    placeholder_dir = dataset_dir / "placeholder"
    semantic_dir = dataset_dir / "semantic"
    generate_dir = dataset_dir / "generate"
    apply_dir = dataset_dir / "apply"
    final_dir = dataset_dir / "final"
    pptx_path = final_dir / "final.pptx"
    placeholders_path = placeholder_dir / "image_placeholders.json"
    config_path = (root / args.config).resolve()

    placeholder_dir.mkdir(parents=True, exist_ok=True)
    semantic_dir.mkdir(parents=True, exist_ok=True)
    generate_dir.mkdir(parents=True, exist_ok=True)
    apply_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    placeholder_script = root / "src" / "svg_image_placeholder.py"
    generate_script = root / "src" / "svg_image_generate.py"
    apply_script = root / "src" / "svg_image_apply.py"
    semantic_script = root / "src" / "svg_text_semantic.py"
    pptx_script = root / "src" / "svg_to_pptx_pro.py"

    if not args.skip_placeholder:
        run_cmd(
            [
                sys.executable,
                str(placeholder_script),
                "--input",
                str(input_dir),
                "--output",
                str(placeholder_dir),
                "--config",
                str(config_path),
            ],
            root,
        )

    if not args.skip_semantic:
        run_cmd(
            [
                sys.executable,
                str(semantic_script),
                "--input",
                str(placeholder_dir),
                "--output",
                str(semantic_dir),
                "--config",
                str(config_path),
                "--workers",
                "4",
                "--qps",
                "4",
                "--timeout",
                "120",
            ],
            root,
        )

    if not args.skip_generate:
        run_cmd(
            [
                sys.executable,
                str(generate_script),
                "--input",
                str(placeholder_dir),
                "--output",
                str(generate_dir),
                "--config",
                str(config_path),
                "--workers",
                "8",
                "--qps",
                "0",
            ],
            root,
        )
        validate_generation_outputs(generate_dir)

    if not args.skip_apply:
        run_cmd(
            [
                sys.executable,
                str(apply_script),
                "--input",
                str(semantic_dir if not args.skip_semantic else placeholder_dir),
                "--output",
                str(apply_dir),
                "--generated",
                str(generate_dir / "generated_images"),
                "--manifest",
                str(generate_dir / "generated_manifest.json"),
                "--placeholder-ref",
                str(placeholder_dir),
                "--config",
                str(config_path),
            ],
            root,
        )

    if not args.skip_pptx:
        run_cmd(
            [
                sys.executable,
                str(pptx_script),
                "--input",
                str(apply_dir),
                "--output",
                str(pptx_path),
                "--placeholders",
                str(placeholders_path),
                "--config",
                str(config_path),
            ],
            root,
        )


if __name__ == "__main__":
    main()
