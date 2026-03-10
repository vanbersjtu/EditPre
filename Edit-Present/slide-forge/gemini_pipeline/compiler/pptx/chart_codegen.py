"""Chart code generation/execution helpers for SVG placeholders."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

try:
    from pptx.chart.data import CategoryChartData, XyChartData, BubbleChartData
except Exception:
    CategoryChartData = None
    XyChartData = None
    BubbleChartData = None

try:
    from pptx.enum.chart import XL_CHART_TYPE
except Exception:
    XL_CHART_TYPE = None

from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt


CHART_CODE_PROMPT = """You are a Python code generator. Generate python-pptx code to create a native PowerPoint chart based on this description:

Chart Description: {caption}
Chart Spec JSON (preferred source of truth): {chart_spec}

Requirements:
1. Generate ONLY a Python function named `add_chart_to_slide(slide, left, top, width, height)`
2. The function should use python-pptx to add a chart to the slide
3. Use the provided left, top, width, height parameters (already in Inches)
4. Import statements are NOT needed - CategoryChartData, XyChartData, BubbleChartData, XL_CHART_TYPE, RGBColor, Pt are available
5. Extract data values and labels from the description
6. Return ONLY the function code, no explanations, no markdown code blocks
7. Make sure the chart matches the description (bar chart, line chart, pie chart, etc.)
8. If chart_spec provides concrete data (categories/series/values/colors), follow chart_spec strictly. Use caption only to fill missing fields.

IMPORTANT RESTRICTIONS - DO NOT use these unsupported attributes:
- chart.fill (Chart has no fill attribute)
- chart.plot_area.format.fill (not supported)
- chart.chart_area (not supported)
- Any background color settings on the chart itself
- Do not reference undefined classes or missing imports

Only use these SAFE attributes:
- chart.series[i].format.fill.solid() and .fore_color.rgb for bar/column colors
- chart.value_axis / chart.category_axis for axis settings
- chart.has_legend for legend toggle
- axis.tick_labels.font for font settings

Example output format:
def add_chart_to_slide(slide, left, top, width, height):
    chart_data = CategoryChartData()
    chart_data.categories = ['A', 'B', 'C']
    chart_data.add_series('Series 1', (1, 2, 3))
    chart = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, left, top, width, height, chart_data
    ).chart
    # Set bar color
    series = chart.series[0]
    series.format.fill.solid()
    series.format.fill.fore_color.rgb = RGBColor(0, 128, 128)
"""


def generate_chart_code(
    caption: str,
    chart_spec: Optional[Any] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: int = 5000,
    temperature: float = 0.2,
    error_hint: Optional[str] = None,
) -> Optional[str]:
    """Call code model to generate python-pptx chart code from caption."""
    import urllib.error
    import urllib.request

    use_model = model or "gemini-3.1-pro-preview"
    use_base_url = base_url or "https://cdn.12ai.org/v1"
    use_base = str(use_base_url).strip().lower()
    is_google_endpoint = (
        "googleapis.com" in use_base
        or "ai.google.dev" in use_base
        or "generativelanguage.googleapis.com" in use_base
        or "aiplatform.googleapis.com" in use_base
    )
    if api_key:
        key = api_key
    elif is_google_endpoint:
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    else:
        # OpenAI-compatible gateways should use OPENAI_API_KEY by default.
        key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("Warning: API key not set. Skipping chart generation.")
        return None

    try:
        chart_spec_text = "null"
        if chart_spec is not None:
            try:
                chart_spec_text = json.dumps(chart_spec, ensure_ascii=False)
            except Exception:
                chart_spec_text = str(chart_spec)
        user_prompt = CHART_CODE_PROMPT.format(caption=caption, chart_spec=chart_spec_text)
        if error_hint:
            user_prompt = (
                user_prompt
                + "\n\nPrevious attempt failed with error:\n"
                + str(error_hint)
                + "\nFix the code and regenerate."
            )

        request_body = {
            "model": use_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a Python code generator specializing in python-pptx charts. Generate code that creates charts using python-pptx library.",
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        url = f"{use_base_url}/chat/completions"
        payload = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url=url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode("utf-8")

        data = json.loads(raw)

        if "error" in data:
            err = data["error"]
            raise RuntimeError(f"API error: {err.get('message', 'unknown')}")

        code = data["choices"][0]["message"]["content"].strip()
        if code.startswith("```"):
            lines = code.split("\n")
            code = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        return code
    except Exception as e:
        print(f"Warning: Failed to generate chart code: {e}")
        return None


def execute_chart_code(
    code: str,
    slide: Any,
    left: float,
    top: float,
    width: float,
    height: float,
) -> Tuple[bool, Optional[str]]:
    """Safely execute generated chart code."""
    exec_globals = {
        "CategoryChartData": CategoryChartData,
        "XL_CHART_TYPE": XL_CHART_TYPE,
        "RGBColor": RGBColor,
        "Pt": Pt,
        "Inches": Inches,
    }
    if XyChartData is not None:
        exec_globals["XyChartData"] = XyChartData
    if BubbleChartData is not None:
        exec_globals["BubbleChartData"] = BubbleChartData
    exec_locals: Dict[str, Any] = {}

    try:
        exec(code, exec_globals, exec_locals)
        if "add_chart_to_slide" in exec_locals:
            exec_locals["add_chart_to_slide"](slide, left, top, width, height)
            return True, None
        return False, "Generated code does not contain add_chart_to_slide function."
    except Exception as e:
        return False, str(e)
