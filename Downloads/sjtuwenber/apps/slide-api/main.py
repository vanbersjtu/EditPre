from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitz
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parents[1]
PIPELINE_ROOT = REPO_ROOT / "gemini_pipeline"
PIPELINE_SCRIPT = PIPELINE_ROOT / "gemini_svg_pipeline.py"
RUNTIME_CONFIG_PATH = PIPELINE_ROOT / "config" / "runtime_api_config.json"
RUNTIME_CONFIG_EXAMPLE_PATH = PIPELINE_ROOT / "config" / "runtime_api_config.example.json"
JOBS_ROOT = PIPELINE_ROOT / "app_data" / "jobs"

MAX_PDF_MB = int(os.environ.get("SLIDE_APP_MAX_PDF_MB", "64"))
PDF_RENDER_SCALE = float(os.environ.get("SLIDE_APP_PDF_RENDER_SCALE", "2.0"))
PIPELINE_MAX_CONCURRENT = int(os.environ.get("SLIDE_APP_MAX_CONCURRENT", "8"))
JOB_LOG_TAIL = int(os.environ.get("SLIDE_APP_LOG_TAIL_LINES", "80"))
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("SLIDE_APP_CORS_ORIGINS", "*").split(",")
    if origin.strip()
]
if not CORS_ORIGINS:
    CORS_ORIGINS = ["*"]

GOOGLE_NATIVE_DEFAULT_API_BASE = os.environ.get(
    "SLIDE_APP_GEMINI_API_BASE",
    "https://generativelanguage.googleapis.com/v1beta",
).strip()
OPENAI_COMPAT_DEFAULT_API_BASE = os.environ.get(
    "SLIDE_APP_OPENAI_API_BASE",
    "https://cdn.12ai.org/v1",
).strip()
DEFAULT_CODE_MODEL = os.environ.get(
    "SLIDE_APP_DEFAULT_MODEL",
    "gemini-3.1-pro-preview",
).strip()
DEFAULT_IMAGE_MODEL = os.environ.get(
    "SLIDE_APP_IMAGE_MODEL",
    "gemini-3.1-flash-image-preview",
).strip()

JOBS_ROOT.mkdir(parents=True, exist_ok=True)
ALLOWED_REFILL_MODES = {"off", "source-crop", "gemini", "auto"}
ALLOWED_REQUEST_PROVIDERS = {"openai-compatible", "gemini-native"}
SENSITIVE_RUNTIME_KEYS = {"OPENAI_API_KEY", "GEMINI_API_KEY", "api_key", "image_api_key"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip())
    base = re.sub(r"-+", "-", base).strip("-._")
    return base or "slides"


def natural_sort_key(path: Path) -> List[Any]:
    parts = re.split(r"(\d+)", path.name)
    key: List[Any] = []
    for part in parts:
        key.append(int(part) if part.isdigit() else part.lower())
    return key


def load_json_dict(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _is_google_api_base(api_base: str) -> bool:
    base = str(api_base or "").strip().lower()
    if not base:
        return False
    return any(
        token in base
        for token in (
            "generativelanguage.googleapis.com",
            "aiplatform.googleapis.com",
            "ai.google.dev",
            "googleapis.com",
        )
    )


def normalize_request_provider(provider: str) -> str:
    raw = str(provider or "").strip().lower()
    if raw in {"gemini", "gemini-native", "google", "google-native"}:
        return "gemini-native"
    return "openai-compatible"


def load_runtime_template() -> Dict[str, Any]:
    config = load_json_dict(RUNTIME_CONFIG_PATH)
    if config:
        return config
    return load_json_dict(RUNTIME_CONFIG_EXAMPLE_PATH)


def resolve_default_request_provider(template: Dict[str, Any]) -> str:
    base = str(template.get("DEFAULT_API_BASE") or template.get("base_url") or "").strip()
    if _is_google_api_base(base):
        return "gemini-native"
    return "openai-compatible"


def resolve_default_request_api_base(provider: str, template: Dict[str, Any]) -> str:
    normalized = normalize_request_provider(provider)
    if normalized == "gemini-native":
        return (
            str(template.get("GEMINI_API_BASE") or template.get("GOOGLE_API_BASE") or "").strip()
            or GOOGLE_NATIVE_DEFAULT_API_BASE
        )
    return (
        str(template.get("DEFAULT_API_BASE") or template.get("base_url") or "").strip()
        or OPENAI_COMPAT_DEFAULT_API_BASE
    )


def resolve_default_code_model(template: Dict[str, Any]) -> str:
    return (
        str(template.get("DEFAULT_MODEL") or template.get("chart_model") or "").strip()
        or DEFAULT_CODE_MODEL
    )


def resolve_default_image_model(template: Dict[str, Any]) -> str:
    return (
        str(template.get("IMAGE_MODEL") or template.get("image_model") or "").strip()
        or DEFAULT_IMAGE_MODEL
    )


def frontend_runtime_defaults() -> Dict[str, Any]:
    template = load_runtime_template()
    request_provider = resolve_default_request_provider(template)
    return {
        "requestProvider": request_provider,
        "requestApiBase": resolve_default_request_api_base(request_provider, template),
        "openaiCompatibleApiBase": resolve_default_request_api_base("openai-compatible", template),
        "geminiNativeApiBase": resolve_default_request_api_base("gemini-native", template),
        "defaultModel": resolve_default_code_model(template),
        "imageModel": resolve_default_image_model(template),
        "refillMode": "source-crop",
    }


def job_dir(job_id: str) -> Path:
    return JOBS_ROOT / job_id


def job_meta_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.json"


def job_log_path(job_id: str) -> Path:
    return job_dir(job_id) / "job.log"


def job_runtime_config_path(job_id: str) -> Path:
    return job_dir(job_id) / "runtime_config.json"


def load_job_meta(job_id: str) -> Dict[str, Any]:
    path = job_meta_path(job_id)
    if not path.exists():
        raise FileNotFoundError(job_id)
    return json.loads(path.read_text(encoding="utf-8"))


def save_job_meta(job_id: str, meta: Dict[str, Any]) -> None:
    meta["updatedAt"] = utc_now()
    job_meta_path(job_id).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_job_meta(job_id: str, **patch: Any) -> Dict[str, Any]:
    meta = load_job_meta(job_id)
    meta.update(patch)
    save_job_meta(job_id, meta)
    return meta


def append_job_log(job_id: str, line: str) -> None:
    with job_log_path(job_id).open("a", encoding="utf-8") as fh:
        fh.write(line)
        if not line.endswith("\n"):
            fh.write("\n")


def tail_job_log(job_id: str, line_count: int = JOB_LOG_TAIL) -> List[str]:
    path = job_log_path(job_id)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return lines[-line_count:]


def render_pdf_pages(pdf_path: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    try:
        for index, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=fitz.Matrix(PDF_RENDER_SCALE, PDF_RENDER_SCALE), alpha=False)
            out_path = out_dir / f"slide_{index:04d}.png"
            pix.save(str(out_path))
        return doc.page_count
    finally:
        doc.close()


def write_job_runtime_config(
    *,
    job_id: str,
    request_provider: str,
    request_api_base: str,
    request_api_key: str,
    default_model: str,
    image_model: str,
) -> Path:
    template = json.loads(json.dumps(load_runtime_template(), ensure_ascii=False))
    config = template if isinstance(template, dict) else {}
    normalized_provider = normalize_request_provider(request_provider)
    resolved_api_base = request_api_base.strip() or resolve_default_request_api_base(normalized_provider, config)
    resolved_default_model = default_model.strip() or resolve_default_code_model(config)
    resolved_image_model = image_model.strip() or resolve_default_image_model(config)
    resolved_api_key = request_api_key.strip()

    config["DEFAULT_API_BASE"] = resolved_api_base
    config["IMAGE_API_BASE"] = resolved_api_base
    config["DEFAULT_MODEL"] = resolved_default_model
    config["IMAGE_MODEL"] = resolved_image_model
    config["base_url"] = resolved_api_base
    config["chart_model"] = resolved_default_model
    config["image_api_base"] = resolved_api_base
    config["image_model"] = resolved_image_model
    config["request_provider"] = normalized_provider

    if normalized_provider == "gemini-native":
        inherited_key = str(
            config.get("GEMINI_API_KEY")
            or config.get("OPENAI_API_KEY")
            or config.get("api_key")
            or ""
        ).strip()
        effective_key = resolved_api_key or inherited_key
        config["GEMINI_API_KEY"] = effective_key
        config["api_key"] = effective_key
        config["image_api_key"] = effective_key
        config.setdefault("OPENAI_API_KEY", "")
    else:
        inherited_key = str(
            config.get("OPENAI_API_KEY")
            or config.get("api_key")
            or config.get("GEMINI_API_KEY")
            or ""
        ).strip()
        effective_key = resolved_api_key or inherited_key
        config["OPENAI_API_KEY"] = effective_key
        config["api_key"] = effective_key
        config["image_api_key"] = effective_key

    path = job_runtime_config_path(job_id)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def redact_runtime_config(job_id: str) -> None:
    path = job_runtime_config_path(job_id)
    if not path.exists():
        return
    data = load_json_dict(path)
    if not data:
        return
    changed = False
    for key in SENSITIVE_RUNTIME_KEYS:
        if key in data and data.get(key):
            data[key] = ""
            changed = True
    if changed:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_pipeline_command(meta: Dict[str, Any]) -> List[str]:
    current_job_dir = job_dir(meta["jobId"])
    config_path = job_runtime_config_path(meta["jobId"])
    if not config_path.exists():
        config_path = RUNTIME_CONFIG_PATH
    command = [
        sys.executable,
        str(PIPELINE_SCRIPT),
        "--input-dir",
        str(current_job_dir / "input"),
        "--output-svg-dir",
        str(current_job_dir / "output" / "svg"),
        "--output-pptx-dir",
        str(current_job_dir / "output" / "pptx"),
        "--profile",
        "slide",
        "--config",
        str(config_path),
        "--max-concurrent",
        str(PIPELINE_MAX_CONCURRENT),
    ]
    refill_mode = str(meta.get("refillMode", "source-crop")).strip().lower()
    if refill_mode != "off":
        command.extend(["--refill-placeholders", "--refill-mode", refill_mode])
    return command


def collect_png_pages(job_id: str, meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    png_root = job_dir(job_id) / "input" / meta["deckStem"]
    pages: List[Dict[str, Any]] = []
    if not png_root.exists():
        return pages
    for png_path in sorted(png_root.glob("*.png"), key=natural_sort_key):
        match = re.search(r"(\d+)", png_path.stem)
        page_num = int(match.group(1)) if match else len(pages) + 1
        pages.append(
            {
                "name": png_path.name,
                "pageNumber": page_num,
                "url": f"/api/jobs/{job_id}/pngs/{png_path.name}",
            }
        )
    return pages


def collect_svg_pages(job_id: str, meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    svg_root = job_dir(job_id) / "output" / "svg" / meta["deckStem"]
    pages: List[Dict[str, Any]] = []
    if not svg_root.exists():
        return pages
    for svg_path in sorted(svg_root.glob("*.svg"), key=natural_sort_key):
        match = re.search(r"(\d+)", svg_path.stem)
        page_num = int(match.group(1)) if match else len(pages) + 1
        pages.append(
            {
                "name": svg_path.name,
                "pageNumber": page_num,
                "url": f"/api/jobs/{job_id}/svgs/{svg_path.name}",
            }
        )
    return pages


def find_pptx_path(job_id: str, meta: Dict[str, Any]) -> Optional[Path]:
    expected = job_dir(job_id) / "output" / "pptx" / f"{meta['deckStem']}.pptx"
    if expected.exists():
        return expected
    pptx_root = job_dir(job_id) / "output" / "pptx"
    candidates = sorted(pptx_root.rglob("*.pptx"), key=natural_sort_key)
    return candidates[0] if candidates else None


def serialize_job(job_id: str) -> Dict[str, Any]:
    meta = load_job_meta(job_id)
    defaults = frontend_runtime_defaults()
    png_pages = collect_png_pages(job_id, meta)
    svg_pages = collect_svg_pages(job_id, meta)
    pptx_path = find_pptx_path(job_id, meta)
    return {
        "jobId": job_id,
        "status": meta.get("status", "queued"),
        "stage": meta.get("stage", "queued"),
        "sourceFilename": meta.get("sourceFilename", "input.pdf"),
        "originalFilename": meta.get("originalFilename"),
        "deckStem": meta.get("deckStem", "slides"),
        "createdAt": meta.get("createdAt"),
        "updatedAt": meta.get("updatedAt"),
        "error": meta.get("error"),
        "pageCount": meta.get("pageCount", 0),
        "sourcePdfUrl": f"/api/jobs/{job_id}/source",
        "pngCount": len(png_pages),
        "pngPages": png_pages,
        "svgCount": len(svg_pages),
        "svgPages": svg_pages,
        "pptxUrl": f"/api/jobs/{job_id}/pptx" if pptx_path else None,
        "artifacts": {
            "sourcePdf": meta.get("sourceFilename"),
            "inputPngDir": f"input/{meta.get('deckStem', 'slides')}",
            "outputSvgDir": f"output/svg/{meta.get('deckStem', 'slides')}",
            "outputPptx": f"output/pptx/{pptx_path.name}" if pptx_path else None,
            "runtimeConfig": "runtime_config.json",
            "persistedOnServer": True,
        },
        "settings": {
            "refillMode": meta.get("refillMode", defaults["refillMode"]),
            "requestProvider": meta.get("requestProvider", defaults["requestProvider"]),
            "requestApiBase": meta.get("requestApiBase", defaults["requestApiBase"]),
            "defaultModel": meta.get("defaultModel", defaults["defaultModel"]),
            "imageModel": meta.get("imageModel", defaults["imageModel"]),
        },
        "logTail": tail_job_log(job_id),
    }


def run_job(job_id: str) -> None:
    meta = load_job_meta(job_id)
    current_job_dir = job_dir(job_id)
    pdf_path = current_job_dir / "source" / meta["sourceFilename"]
    png_dir = current_job_dir / "input" / meta["deckStem"]
    try:
        update_job_meta(job_id, status="running", stage="rendering-pdf", error=None)
        append_job_log(job_id, f"[{utc_now()}] Rendering PDF pages")
        page_count = render_pdf_pages(pdf_path, png_dir)
        update_job_meta(job_id, pageCount=page_count)
        append_job_log(job_id, f"[{utc_now()}] Rendered {page_count} PNG pages")

        update_job_meta(job_id, status="running", stage="generating-svg-pptx")
        cmd = build_pipeline_command(meta)
        append_job_log(job_id, f"[{utc_now()}] Running pipeline: {' '.join(cmd)}")

        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            append_job_log(job_id, line.rstrip("\n"))
        return_code = proc.wait()
        if return_code != 0:
            raise RuntimeError(f"Pipeline exited with code {return_code}")

        pptx_path = find_pptx_path(job_id, meta)
        if pptx_path is None:
            raise FileNotFoundError("PPTX output not found after pipeline run")
        append_job_log(job_id, f"[{utc_now()}] Completed: {pptx_path}")
        update_job_meta(job_id, status="succeeded", stage="done", error=None)
    except Exception as exc:  # noqa: BLE001
        append_job_log(job_id, f"[{utc_now()}] ERROR: {exc}")
        append_job_log(job_id, traceback.format_exc())
        update_job_meta(job_id, status="failed", stage="failed", error=str(exc))
    finally:
        redact_runtime_config(job_id)


app = FastAPI(title="Slide Pipeline API", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "jobsRoot": str(JOBS_ROOT),
        "pipelineScript": str(PIPELINE_SCRIPT),
        "runtimeConfig": str(RUNTIME_CONFIG_PATH),
        "runtimeDefaults": frontend_runtime_defaults(),
        "allowedRefillModes": sorted(ALLOWED_REFILL_MODES),
        "allowedRequestProviders": sorted(ALLOWED_REQUEST_PROVIDERS),
    }


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    refill_mode: str = Form("source-crop"),
    request_provider: str = Form("openai-compatible"),
    request_api_base: str = Form(""),
    request_api_key: str = Form(""),
    default_model: str = Form(""),
    image_model: str = Form(""),
) -> Dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing PDF filename")
    suffix = Path(file.filename).suffix.lower()
    if suffix != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Uploaded PDF is empty")
    if len(payload) > MAX_PDF_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"PDF exceeds {MAX_PDF_MB} MB limit")

    refill_mode = refill_mode.strip().lower()
    if refill_mode not in ALLOWED_REFILL_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported refill_mode '{refill_mode}'. Allowed: {sorted(ALLOWED_REFILL_MODES)}",
        )

    request_provider = normalize_request_provider(request_provider)
    if request_provider not in ALLOWED_REQUEST_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported request_provider '{request_provider}'. Allowed: {sorted(ALLOWED_REQUEST_PROVIDERS)}",
        )

    template = load_runtime_template()
    resolved_request_api_base = request_api_base.strip() or resolve_default_request_api_base(request_provider, template)
    resolved_default_model = default_model.strip() or resolve_default_code_model(template)
    resolved_image_model = image_model.strip() or resolve_default_image_model(template)

    deck_stem = slugify(Path(file.filename).stem)
    job_id = uuid.uuid4().hex[:12]
    current_job_dir = job_dir(job_id)
    (current_job_dir / "source").mkdir(parents=True, exist_ok=True)
    (current_job_dir / "input").mkdir(parents=True, exist_ok=True)
    (current_job_dir / "output" / "svg").mkdir(parents=True, exist_ok=True)
    (current_job_dir / "output" / "pptx").mkdir(parents=True, exist_ok=True)

    pdf_name = f"{deck_stem}.pdf"
    pdf_path = current_job_dir / "source" / pdf_name
    pdf_path.write_bytes(payload)
    runtime_config_path = write_job_runtime_config(
        job_id=job_id,
        request_provider=request_provider,
        request_api_base=resolved_request_api_base,
        request_api_key=request_api_key,
        default_model=resolved_default_model,
        image_model=resolved_image_model,
    )

    meta = {
        "jobId": job_id,
        "status": "queued",
        "stage": "queued",
        "sourceFilename": pdf_name,
        "originalFilename": file.filename,
        "deckStem": deck_stem,
        "refillMode": refill_mode,
        "requestProvider": request_provider,
        "requestApiBase": resolved_request_api_base,
        "defaultModel": resolved_default_model,
        "imageModel": resolved_image_model,
        "pageCount": 0,
        "createdAt": utc_now(),
        "updatedAt": utc_now(),
        "error": None,
    }
    save_job_meta(job_id, meta)
    append_job_log(job_id, f"[{utc_now()}] Accepted upload: {file.filename}")
    append_job_log(job_id, f"[{utc_now()}] Refill mode: {refill_mode}")
    append_job_log(job_id, f"[{utc_now()}] Request provider: {request_provider}")
    append_job_log(job_id, f"[{utc_now()}] Request API base: {resolved_request_api_base}")
    append_job_log(job_id, f"[{utc_now()}] Code model: {resolved_default_model}")
    append_job_log(job_id, f"[{utc_now()}] Image model: {resolved_image_model}")
    append_job_log(job_id, f"[{utc_now()}] Request API key provided: {'yes' if request_api_key.strip() else 'no'}")
    append_job_log(job_id, f"[{utc_now()}] Job workspace: {current_job_dir}")
    append_job_log(job_id, f"[{utc_now()}] Runtime config: {runtime_config_path}")
    append_job_log(job_id, f"[{utc_now()}] Source PDF: {pdf_path}")
    append_job_log(job_id, f"[{utc_now()}] Input PNG dir: {current_job_dir / 'input' / deck_stem}")
    append_job_log(job_id, f"[{utc_now()}] Output SVG dir: {current_job_dir / 'output' / 'svg' / deck_stem}")
    append_job_log(job_id, f"[{utc_now()}] Output PPTX dir: {current_job_dir / 'output' / 'pptx'}")
    append_job_log(job_id, f"[{utc_now()}] Runtime config keys will be redacted after execution")

    worker = threading.Thread(target=run_job, args=(job_id,), daemon=True)
    worker.start()
    return serialize_job(job_id)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    try:
        return serialize_job(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@app.get("/api/jobs/{job_id}/source")
def get_source_pdf(job_id: str) -> FileResponse:
    meta = load_job_meta(job_id)
    pdf_path = job_dir(job_id) / "source" / meta["sourceFilename"]
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Source PDF not found")
    return FileResponse(pdf_path, media_type="application/pdf", filename=pdf_path.name)


@app.get("/api/jobs/{job_id}/pngs/{png_name}")
def get_png(job_id: str, png_name: str) -> FileResponse:
    meta = load_job_meta(job_id)
    png_path = job_dir(job_id) / "input" / meta["deckStem"] / png_name
    if not png_path.exists():
        raise HTTPException(status_code=404, detail="PNG not found")
    return FileResponse(png_path, media_type="image/png", filename=png_name)


@app.get("/api/jobs/{job_id}/svgs/{svg_name}")
def get_svg(job_id: str, svg_name: str) -> FileResponse:
    meta = load_job_meta(job_id)
    svg_path = job_dir(job_id) / "output" / "svg" / meta["deckStem"] / svg_name
    if not svg_path.exists():
        raise HTTPException(status_code=404, detail="SVG not found")
    return FileResponse(svg_path, media_type="image/svg+xml", filename=svg_name)


@app.get("/api/jobs/{job_id}/pptx")
def get_pptx(job_id: str) -> FileResponse:
    meta = load_job_meta(job_id)
    pptx_path = find_pptx_path(job_id, meta)
    if pptx_path is None or not pptx_path.exists():
        raise HTTPException(status_code=404, detail="PPTX not found")
    return FileResponse(
        pptx_path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=pptx_path.name,
    )


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> Dict[str, Any]:
    current_job_dir = job_dir(job_id)
    if not current_job_dir.exists():
        raise HTTPException(status_code=404, detail="Job not found")
    shutil.rmtree(current_job_dir)
    return {"ok": True, "jobId": job_id}
