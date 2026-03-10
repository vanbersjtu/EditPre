import argparse
import base64
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Tuple

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None
    genai_types = None

try:
    from compiler.io.config_loader import load_config as load_runtime_config
except Exception:
    def load_runtime_config(config_path: Optional[Path]) -> dict:
        if not config_path or not config_path.exists():
            return {}
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

try:
    from compiler.io.profile_loader import (
        DEFAULT_PROFILE as DEFAULT_TASK_PROFILE,
        SUPPORTED_PROFILES,
        load_profile_spec,
        resolve_profile_prompt_file,
    )
except Exception:
    DEFAULT_TASK_PROFILE = "slide"
    SUPPORTED_PROFILES = ("slide", "figure", "poster")
    load_profile_spec = None
    resolve_profile_prompt_file = None


MODEL = "gemini-3-pro-preview"

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RUNTIME_CONFIG_PATH = SCRIPT_DIR / "config" / "runtime_api_config.json"
DEFAULT_INPUT_DIR = SCRIPT_DIR / "input"
DEFAULT_OUTPUT_SVG_DIR = SCRIPT_DIR / "output" / "svg"
DEFAULT_OUTPUT_PPTX_DIR = SCRIPT_DIR / "output" / "pptx"
CONVERTER_SCRIPT = SCRIPT_DIR / "svg_to_pptx_pro.py"

# 默认提示词：已填入 prompt.md 的完整版本
PROMPT_TEXT = """
# Role & Objective
你是一个世界顶级的无代码 UI 工程师和多模态视觉数据分析专家。你的任务是将用户输入的一张 PPT 幻灯片图像，100% 视觉还原并转换为包含高度语义化标注的完整 SVG 代码。

输入：一张 PPT 幻灯片图像。
输出：一段标准的、可以直接渲染的 SVG 代码（只输出代码，不需要多余的解释）。

为了实现完美的视觉还原与语义理解，你必须严格遵守以下三大核心任务的规范：

---

## Task 1: 矢量图形绘制与占位符策略 (Vector Graphics & Placeholding Strategy)

**核心原则：矢量优先，占位符为辅**

你必须按照以下优先级顺序处理所有非文本视觉元素：

### 优先级 1：矢量路径绘制（首选）
对于以下类型的视觉元素，**必须使用 SVG 矢量路径/形状绘制**：
- **简单几何图形**：矩形、圆形、椭圆、多边形、线条等
- **装饰性元素**：色块、背景形状、分割线、边框等
- **图标和简单插图**：扁平化图标、简笔画、几何风格的插图
- **文字背景**：色块背景、高亮区域等

注意：图表主体不在本优先级内，图表必须遵守下方“图表专用硬规则”。

**绘制要求**：
- 使用 `<rect>`, `<circle>`, `<ellipse>`, `<path>`, `<line>`, `<polyline>`, `<polygon>` 等 SVG 基本形状
- 精确还原颜色（fill/stroke）、位置（x/y）、尺寸（width/height）、圆角（rx/ry）等属性
- 对于复杂路径，使用 `<path d="...">` 精确描述

### 优先级 2：占位符（仅在必要时使用）
**只有当满足以下条件时**，才使用占位符：
- **复杂照片**：真实人物照片、风景照片、实物照片等无法用矢量表达的内容
- **超复杂插图**：极其复杂的艺术插画，矢量路径会超过 1000 行代码
- **复杂数据图表**：包含很多数据点的散点图、热力图等

**占位符生成规则**：
1. 在对应坐标位置，生成 `<g>` 标签作为占位，并带上特定属性：
   - `data-type="image-placeholder"`
   - `data-caption="..."`：**必须使用英文**，详尽描述该区域的画面内容，作为生图模型的 Prompt（最大长度 512 tokens）。
   - `data-is-chart="true|false"`：根据内容严格判定。如果是纯数据可视化图表（柱状图、折线图、饼图、散点图等），值为 true；如果是普通照片或插画，值为 false。
   - `data-chart-spec="...json..."`：当 `data-is-chart="true"` 时必须提供，内容为结构化图表规格（chart_type、categories、series、values、colors、title、axes、legend、data_labels 等）。
   - `data-remove-bg="true|false"`：是否需要在后处理时做 RGBA 去背景。  
     - `true`：主体不规则、需要透明边缘贴合背景（如抠图感图标/人物前景/贴纸类插图）。
     - `false`：原图本身是完整矩形画面（照片、示意图、截图、海报），应保留矩形边界，禁止去背景。
   - `data-text-policy="editable|raster"`：该占位区域文本处理策略。  
     - `editable`（默认）：文本尽量走 semantic-layer，可编辑。  
     - `raster`：文本与图形强绑定（如地图标注/复杂信息图 callout），文本保留在图片里，后续不再叠加 semantic 文本。
     - 只要区域内存在“与图形强绑定的标签文本”（地图地名、连线注释、图中小字说明），优先设为 `raster`。
2. 占位符内部包含一个背景矩形代表大小。

### 图表专用硬规则（必须遵守）
- 凡是图表区域（bar/line/pie/scatter/area/histogram/radar 等），**图表主体必须只输出 `image-placeholder`**，禁止输出图表 path/rect/polyline/circle 等数据图元细节。
- 图表主体包括：坐标轴、网格线、柱/线/点、图例色块、数据标签、刻度标签等。
- 允许保留图表外围装饰为普通矢量：卡片背景、边框、阴影、标题栏装饰、分割线等。
- 图表标题/副标题/说明文字建议继续进入 semantic-layer（可编辑文本）。

### 图文强绑定区域规则（必须遵守）
- 当某区域采用 `data-text-policy="raster"` 时：
  - 该区域中的图内标签文本必须保留在 image-placeholder 对应图像内容里；
  - semantic-layer 中禁止再次输出该区域同一批标签文本（避免双层文本重叠）。
- 典型适用场景：地图标注、流程示意图内部注释、信息图 callout、带引线的图内说明。

**互斥硬规则（必须遵守）**：
- **同一空间区域二选一**：要么输出真实矢量图形（`path/rect/circle/line/text...`），要么输出 `image-placeholder`，**绝对禁止同时存在**。
- 如果你已经在该 `<g>` 中画出了图标/插图主体（例如包含多个 `path/circle/line/text` 组成完整图形），则该 `<g>` **不得**标记为 `data-type="image-placeholder"`，也不得携带 `data-caption`。
- `image-placeholder` 仅用于“后续生图/回填”的空位，内部只允许保留简化占位底框（如一个背景 `rect`），不允许放完整矢量插图。
- 若不确定，优先走“矢量绘制”，不要打占位符。

**占位符格式示例：**
<g data-type="image-placeholder" data-caption="A realistic photo of a diverse team working in a modern office, bright lighting" data-is-chart="false" data-remove-bg="false" transform="...">
    <rect x="..." y="..." width="..." height="..." fill="#f0f0f0" stroke="#cccccc" />
</g>

<g data-type="image-placeholder"
   data-is-chart="true"
   data-caption="Clustered column chart comparing Q1-Q4 revenue for Product A and Product B"
   data-chart-spec='{"chart_type":"column_clustered","categories":["Q1","Q2","Q3","Q4"],"series":[{"name":"Product A","values":[12,18,15,22],"color":"#1f4e79"},{"name":"Product B","values":[10,14,17,20],"color":"#ed7d31"}],"legend":true,"data_labels":false}'
   data-remove-bg="false">
    <rect x="..." y="..." width="..." height="..." fill="#f0f0f0" stroke="#cccccc" />
</g>

<g data-type="image-placeholder"
   data-is-chart="false"
   data-caption="A world map with city markers and labels"
   data-remove-bg="false"
   data-text-policy="raster">
    <rect x="..." y="..." width="..." height="..." fill="#f0f0f0" stroke="#cccccc" />
</g>

**判断示例**：
- ✅ **应该用矢量绘制**：扁平化插画、几何图形组合的图标、简单背景装饰、色块分隔
- ❌ **应该用占位符**：真实人物照片、复杂风景照片、超写实插画

---

## Task 2: 文本语义层级标注 (Semantic Text Grouping & Annotation)
所有的文本元素不能是散落的 `<text>` 标签，必须被组织进一个名为 `<g id="semantic-layer" data-type="semantic-layer">` 的顶层根节点中，并形成具有深度树结构的语义块。

### 允许的角色 (Roles)
严格限定以下取值：`title`, `subtitle`, `body`, `kpi`, `kpi_unit`, `callout`, `bullet`, `numbered`, `section`, `header`, `footer`, `footnote`, `unknown`。

### 节点类型 (Node Types)
1. **文本框 (textbox)**: 树的叶子节点，包含实际的 `<text>` 元素。
2. **文本块 (group)**: 树的枝干节点，用于将相关的 textbox 组合在一起。

### SVG 结构规范 (严格参考下述 DOM 树)
```xml
<g id="semantic-layer" data-type="semantic-layer">
    <!-- 顶层 Group -->
    <g id="group-1" data-type="text-group" data-role="body" data-order="1" data-confidence="0.9" data-x="10" data-y="10" data-w="200" data-h="100">
        <!-- 每一个 group 和 textbox 的第一个子元素必须是一个透明的 rect，代表它的 BBox -->
        <rect class="tb-bbox" x="10" y="10" width="200" height="100" fill="none" stroke="none" opacity="0" />
        
        <!-- 底层 Textbox -->
        <g id="tb-1" data-type="textbox" data-role="bullet" data-order="1" data-confidence="0.95" data-x="10" data-y="10" data-w="200" data-h="30">
            <rect class="tb-bbox" x="10" y="10" width="200" height="30" fill="none" opacity="0" />
            <text x="10" y="25" font-family="..." font-size="..." fill="...">Your actual text here</text>
        </g>
    </g>
</g>
```
### 文本分组硬性规则（必须严格遵守）

#### 1. 语义完整性原则（最重要）
- **同一语义单元的文本必须合并**：如果多行文本在语义上是一个整体（如 "Canva's Milkshakes Launch" 这样的多行标题），即使视觉上分成多行，也必须合并为**一个 textbox**，使用 `<tspan>` 标签处理换行。
- **错误示例**：将 "Canva's"、"Milkshakes"、"Launch" 分成3个独立的 textbox ❌
- **正确示例**：合并为一个 textbox，内部用3个 `<tspan>` 或3个 `<text>` 元素表示3行 ✅

#### 2. 字号一致性
- 同一个 textbox 内的所有文本项必须字号相同（同一 fontSize）。如果同一段落存在不同字号，必须拆分成多个 textbox 然后套入同一个 group。

#### 3. 段落合并
- 如果多行文本属于同一段落或同一标题块，且字号相同、上下相邻、对齐一致，必须合并为一个 textbox（支持多行 `<tspan>` 或多个 `<text>` 在同一个 textbox 组内）。

#### 4. 主副标题逻辑
- subtitle 只能在字号明显小于 title 时使用；如果字号相同，不要拆成 title + subtitle，必须合并为一个多行标题 textbox (role="title")。

#### 5. Bullet 处理
- 每个 bullet point（如带有项目符号的一条内容）应该至少拆成两个 textbox（小标题 + 正文，如果它们样式或语义分离），并将它们放进一个 group 里（bullet block），其 role 为 bullet。

#### 6. 层级包含
- 若存在主标题/副标题，应与所有 bullet groups 一起归入更高层的总体 group（如一个完整的页面内容区块或卡片）。

#### 7. 阅读顺序 (data-order)
- 先上后下，同一行从左到右，从 1 开始按顺序标注。

### 合并示例（必须遵循）
```xml
<!-- 错误：将多行标题拆成多个 textbox -->
<g id="tb-title-1" data-type="textbox" data-role="title">Canva's</g>
<g id="tb-title-2" data-type="textbox" data-role="title">Milkshakes</g>
<g id="tb-title-3" data-type="textbox" data-role="title">Launch</g>

<!-- 正确：合并为一个 textbox，内部用多个 text 或 tspan -->
<g id="tb-title-1" data-type="textbox" data-role="title">
    <text x="108" y="240" font-size="130">Canva's</text>
    <text x="108" y="380" font-size="130">Milkshakes</text>
    <text x="108" y="520" font-size="130">Launch</text>
</g>
```

---

## Task 3: 视觉布局分组 (Visual Layout Grouping, 必须输出)
除了 semantic-layer 的文本语义树，你还必须输出一个独立的视觉分组层，用于把“同一空间块里的文本+装饰图形”绑定在一起，方便后续整体拖动与编辑。

### 视觉分组层要求
1. 新增根节点：`<g id="visual-layer" data-type="visual-layer"> ... </g>`。
2. 按页面结构划分多个视觉组：`<g id="vg-1" data-type="visual-group" data-role="..." data-order="...">`。
3. 每个 `visual-group` 的第一个子元素必须是透明 bbox：
   - `<rect class="vg-bbox" x="..." y="..." width="..." height="..." fill="none" stroke="none" opacity="0" />`
4. 把该区域内与该语义块相关的装饰元素放入同一个 `visual-group`：
   - 包括 `path / rect / line / circle / polygon / polyline / image-placeholder` 等非文本元素。
   - 注意：这里的 `image-placeholder` 必须是“真实占位符”（空位），不能是已经画好矢量主体的伪占位符。
5. semantic-layer 中对应的 `text-group` 必须增加 `data-visual-group="vg-*"` 引用。
6. 同时，该 `visual-group` 内所有装饰元素也必须写 `data-visual-group="vg-*"`，保证文本组与装饰组可一一映射。
7. 页面级背景元素（整页底色、大背景图形）不要混入局部组，单独放在 `vg-background`（或不打组）。

### 空间分组判定规则（必须遵守）
- 同一视觉卡片/模块内的标题、正文、图标、边框、底纹必须归同组。
- 仅因为颜色相同不能分组；必须同时满足空间邻近和语义关联。
- 组与组之间不重叠；如必须重叠，优先按主语义归属。

## 最终输出要求
**重要：画布尺寸必须固定为 1920x1080 像素！**

SVG 根元素必须使用以下格式：
```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" width="1920" height="1080">
```

100% 视觉还原：画布尺寸（ViewBox）、坐标（x/y）、宽高、颜色、字体大小、字体类型（font-family）、对齐方式等必须尽可能与原图精确吻合。

### 字体处理规范（非常重要！）
1. **仅基于图像视觉判断字体**：不要假设能读取源文件字体元数据；根据字形风格尽量贴近原图。
2. **禁止引用外部字体**：不要使用 `@import`、`@font-face` 或任何外部字体链接（如 Google Fonts）。
3. **字体白名单约束（必须）**：`font-family` 只能从以下集合中选择并组合回退链：
   - 中文无衬线：`PingFang SC`, `Hiragino Sans GB`, `Microsoft YaHei`, `Heiti SC`, `Source Han Sans SC`, `Noto Sans SC`, `sans-serif`
   - 中文衬线：`SimSun`, `STSong`, `Songti SC`, `Source Han Serif SC`, `Noto Serif SC`, `serif`
   - 英文无衬线：`Arial`, `Helvetica`, `Calibri`, `Segoe UI`, `sans-serif`
   - 英文衬线：`Times New Roman`, `Georgia`, `Cambria`, `serif`
4. **添加系统回退链**：在主字体后面添加系统字体作为备选，格式如下：
   - **中文无衬线字体**：`"原字体，Microsoft YaHei, PingFang SC, Heiti SC, sans-serif"`
   - **中文衬线字体**：`"原字体，SimSun, STSong, KaiTi, serif"`
   - **英文无衬线字体**：`"原字体，Arial, Helvetica, sans-serif"`
   - **英文衬线字体**：`"原字体，Times New Roman, Georgia, serif"`
5. **直接在元素上指定字体**：在 `<text>` 元素的 `font-family` 属性中直接指定，不要在 `<style>` 中定义。

### 坐标与变换规范（为保证 SVG -> PPTX 一致）
1. **semantic-layer 内的 textbox 必须使用绝对坐标**：`data-x/data-y/data-w/data-h` 与首个 `rect.tb-bbox` 必须一致，且都以根画布坐标系为准。
2. **semantic-layer 内尽量不要使用 transform**：尤其避免在 `textbox` 或其 `text` 上使用 `scale()`、`matrix()`。
3. **如必须旋转文本**：只允许 `rotate(±90)`，并且仍需提供准确的 `data-x/data-y/data-w/data-h` 与 `tb-bbox`。
4. **禁止对 semantic-layer 祖先组施加 transform**：不要把文本层放在带 `transform` 的父级 `<g>` 里。
5. **普通视觉层（非 semantic-layer）可以使用 transform**：但文本语义层必须满足以上硬约束。

### 示例
```xml
<!-- 正确：保留原字体名称 + 系统回退 -->
<text x="10" y="20" font-family="Noto Sans SC, Microsoft YaHei, PingFang SC, sans-serif" font-size="24">标题</text>
<text x="10" y="20" font-family="Source Han Serif CN, SimSun, STSong, serif" font-size="24">正文</text>
<text x="10" y="20" font-family="Times New Roman, Georgia, serif" font-size="24">English</text>

<!-- 错误：不要使用 @import 引用外部字体 -->
<style>
  @import url('https://fonts.googleapis.com/...');  /* ❌ 禁止这样写 */
  text { font-family: 'Noto Sans SC'; }  /* ❌ 不要在 style 中定义 */
</style>
```

只输出标准 XML/SVG 代码，使用 ```xml 包裹。不需要输出任何其他分析过程、说明或原先的 JSON 格式！直接把占位逻辑和结构化树表现在最终的 DOM 结构中。
"""

# 输出 SVG 文件路径（批处理下会按输入目录结构自动生成）


def build_endpoint(model: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _cli_flag_present(flag: str) -> bool:
    return any(arg == flag or arg.startswith(f"{flag}=") for arg in sys.argv[1:])


def _is_google_api_base(api_base: str) -> bool:
    base = str(api_base or "").strip().lower()
    if not base:
        return True
    return any(
        token in base
        for token in (
            "generativelanguage.googleapis.com",
            "aiplatform.googleapis.com",
            "ai.google.dev",
            "googleapis.com",
        )
    )


def _call_openai_compat(
    api_base: str,
    api_key: str,
    model: str,
    prompt_text: str,
    image_b64: str,
    timeout: int,
    retries: int,
) -> str:
    url = f"{api_base.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text.strip()},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                ],
            }
        ],
        "max_tokens": 32768,
        "temperature": 0.2,
    }
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

    last_err: Optional[Exception] = None
    for i in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url=url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
            data = json.loads(raw)
            if "error" in data:
                err = data.get("error") or {}
                raise RuntimeError(f"OpenAI-compatible API error ({err.get('code')}): {err.get('message')}")
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError("OpenAI-compatible API returned empty choices")
            content = ((choices[0].get("message") or {}).get("content") or "").strip()
            if not content:
                raise RuntimeError("OpenAI-compatible API returned empty content")
            return content
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if i < retries:
                sleep_sec = min(2 ** i, 8)
                print(f"  .. 请求失败，{sleep_sec}s 后重试 ({i}/{retries}): openai_compat={exc}")
                time.sleep(sleep_sec)
    raise RuntimeError(f"openai_compat 失败: {last_err}")


def resolve_profile_prompt_and_model(
    profile: str,
    profile_dir: Optional[Path],
    cli_model: str,
) -> Tuple[str, str]:
    prompt_text = PROMPT_TEXT
    model = cli_model
    loader_missing = load_profile_spec is None
    if loader_missing:
        return prompt_text, model

    try:
        spec = load_profile_spec(profile, profile_dir=profile_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: failed to load profile '{profile}': {exc}")
        return prompt_text, model
    if not spec:
        return prompt_text, model

    pipeline_spec = spec.get("pipeline") if isinstance(spec.get("pipeline"), dict) else {}
    if resolve_profile_prompt_file is not None:
        prompt_file = resolve_profile_prompt_file(spec, SCRIPT_DIR)
        if prompt_file and prompt_file.exists():
            try:
                prompt_text = prompt_file.read_text(encoding="utf-8")
                print(f"Loaded profile prompt: {prompt_file}")
            except Exception as exc:  # noqa: BLE001
                print(f"Warning: failed to read profile prompt file {prompt_file}: {exc}")

    prompt_suffix = str(pipeline_spec.get("prompt_suffix") or "").strip()
    if prompt_suffix:
        prompt_text = f"{prompt_text.rstrip()}\\n{prompt_suffix}\\n"

    profile_model = str(pipeline_spec.get("model") or "").strip()
    if profile_model and (not _cli_flag_present("--model")):
        model = profile_model
        print(f"Profile model override applied: {model}")

    return prompt_text, model


def natural_sort_key(text: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def collect_png_groups(input_dir: Path):
    groups = defaultdict(list)
    png_paths = sorted(
        [
            p for p in input_dir.rglob("*")
            if p.is_file() and p.suffix.lower() == ".png" and not p.name.startswith("._")
        ],
        key=lambda p: natural_sort_key(str(p.relative_to(input_dir))),
    )

    for png_path in png_paths:
        rel_parent = png_path.parent.relative_to(input_dir)
        groups[rel_parent].append(png_path)

    return dict(
        sorted(groups.items(), key=lambda item: natural_sort_key(str(item[0])))
    )


def load_image_as_base64(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_request_body(prompt_text: str, image_b64: str) -> dict:
    return {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt_text.strip(),
                    },
                    {
                        "inline_data": {
                            "mime_type": "image/png",
                            "data": image_b64,
                        }
                    },
                ],
            }
        ],
        "generation_config": {
            "temperature": 0.2,
            "max_output_tokens": 32768,
        },
    }


def extract_text_from_response(data: dict) -> str:
    if "error" in data:
        err = data["error"]
        code = err.get("code")
        msg = err.get("message", "unknown error")
        raise RuntimeError(f"Gemini API error ({code}): {msg}")

    text_parts = []
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            if "text" in part:
                text_parts.append(part["text"])
    text = "".join(text_parts)
    if not text.strip():
        raise RuntimeError(f"Gemini 返回为空，原始响应 keys={list(data.keys())}")
    return text


def call_gemini_with_urllib(api_key: str, endpoint: str, body: dict, timeout: int) -> str:
    url = endpoint
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
    data = json.loads(raw)
    return extract_text_from_response(data)


def call_gemini_with_sdk(api_key: str, model: str, body: dict, timeout: int) -> str:
    if genai is None or genai_types is None:
        raise RuntimeError("google-genai SDK 不可用")

    parts = body.get("contents", [{}])[0].get("parts", [])
    prompt_text = ""
    image_b64 = None
    mime_type = "image/png"
    for part in parts:
        if "text" in part and not prompt_text:
            prompt_text = part["text"]
        inline_data = part.get("inline_data")
        if inline_data and "data" in inline_data:
            image_b64 = inline_data["data"]
            mime_type = inline_data.get("mime_type", mime_type)

    if not prompt_text:
        raise RuntimeError("SDK 请求缺少 prompt 文本")
    if not image_b64:
        raise RuntimeError("SDK 请求缺少图片数据")

    image_bytes = base64.b64decode(image_b64)
    content = genai_types.Content(
        role="user",
        parts=[
            genai_types.Part.from_text(text=prompt_text),
            genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
        ],
    )

    gen_cfg = body.get("generation_config", {})
    config = genai_types.GenerateContentConfig(
        temperature=gen_cfg.get("temperature", 0.2),
        max_output_tokens=gen_cfg.get("max_output_tokens", 32768),
    )

    client = genai.Client(
        api_key=api_key,
        http_options=genai_types.HttpOptions(timeout=timeout),
    )
    response = client.models.generate_content(
        model=model,
        contents=[content],
        config=config,
    )
    text = getattr(response, "text", None) or ""
    if not text.strip():
        raise RuntimeError("SDK 返回为空")
    return text


def call_gemini_with_curl(api_key: str, endpoint: str, body: dict, timeout: int) -> str:
    payload = json.dumps(body, ensure_ascii=False)
    cmd = [
        "curl",
        "-sS",
        "--connect-timeout",
        "20",
        "--max-time",
        str(timeout),
        endpoint,
        "-H",
        "Content-Type: application/json",
        "-H",
        f"x-goog-api-key: {api_key}",
        "-X",
        "POST",
        "-d",
        payload,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"curl 调用失败 (code={proc.returncode}): {proc.stderr.strip()}"
        )
    data = json.loads(proc.stdout)
    return extract_text_from_response(data)


def call_gemini(api_key: str, endpoint: str, model: str, body: dict, retries: int = 3, timeout: int = 300) -> str:
    last_sdk_err = None
    last_curl_err = None
    last_urllib_err = None

    for i in range(1, retries + 1):
        try:
            # urllib is currently the most reliable path in this environment.
            return call_gemini_with_urllib(api_key, endpoint, body, timeout)
        except urllib.error.HTTPError as exc:
            last_urllib_err = exc
            if i < retries:
                sleep_sec = min(2 ** i, 8)
                print(
                    f"  .. 请求失败，{sleep_sec}s 后重试 ({i}/{retries}): "
                    f"urllib={last_urllib_err}"
                )
                time.sleep(sleep_sec)
                continue
        except (urllib.error.URLError, ssl.SSLError, TimeoutError, OSError, RuntimeError) as exc:
            last_urllib_err = exc

        try:
            return call_gemini_with_sdk(api_key, model, body, timeout)
        except Exception as exc:  # noqa: BLE001
            last_sdk_err = exc

        try:
            return call_gemini_with_curl(api_key, endpoint, body, timeout)
        except Exception as exc:  # noqa: BLE001
            last_curl_err = exc

        if i < retries:
            sleep_sec = min(2 ** i, 8)
            print(
                f"  .. 请求失败，{sleep_sec}s 后重试 ({i}/{retries}): "
                f"urllib={last_urllib_err} | sdk={last_sdk_err} | curl={last_curl_err}"
            )
            time.sleep(sleep_sec)

    raise RuntimeError(
        f"urllib失败: {last_urllib_err} | sdk失败: {last_sdk_err} | curl失败: {last_curl_err}"
    )


def extract_svg(text: str) -> str:
    start = text.find("<svg")
    end = text.rfind("</svg>")
    if start == -1 or end == -1:
        return text
    return text[start : end + len("</svg>")]


def _tag_name(elem: ET.Element) -> str:
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


def _classify_placeholder_group(elem: ET.Element) -> str:
    """Classify image-placeholder group.

    Returns:
    - placeholder: true placeholder for later refill
    - vectorized: already contains substantive vector drawing
    """
    force_placeholder = str(elem.get("data-force-placeholder") or "").strip().lower()
    if force_placeholder in ("1", "true", "yes", "on"):
        return "placeholder"

    has_image_href = False
    has_vector_content = False
    for child in elem.iter():
        if child is elem:
            continue
        t = _tag_name(child)
        if t == "image":
            href = (child.get("{http://www.w3.org/1999/xlink}href") or child.get("href") or "").strip()
            if href:
                has_image_href = True
                break
        elif t in ("path", "circle", "ellipse", "line", "polyline", "polygon", "text"):
            has_vector_content = True

    if has_image_href:
        return "placeholder"
    if has_vector_content:
        return "vectorized"
    return "placeholder"


def sanitize_placeholder_groups(svg_text: str) -> str:
    """Auto-fix model outputs that violate placeholder/vector exclusivity rule."""
    try:
        root = ET.fromstring(svg_text)
    except Exception:
        return svg_text

    fixed = 0
    for elem in root.iter():
        if _tag_name(elem) != "g":
            continue
        if (
            elem.get("data-type") != "image-placeholder"
            and elem.get("data-role") != "image-placeholder"
        ):
            continue
        if _classify_placeholder_group(elem) != "vectorized":
            continue

        # Demote fake placeholders (already vectorized) to normal vector groups.
        if elem.get("data-type") == "image-placeholder":
            del elem.attrib["data-type"]
        if elem.get("data-role") == "image-placeholder":
            del elem.attrib["data-role"]
        elem.attrib.pop("data-caption", None)
        elem.attrib.pop("data-is-chart", None)
        elem.attrib.pop("data-chart-spec", None)
        elem.attrib.pop("data-remove-bg", None)
        elem.attrib.pop("data-text-policy", None)
        elem.attrib.pop("data-rgba", None)
        elem.attrib.pop("data-needs-rgba", None)
        elem.set("data-rendered-vector", "true")
        fixed += 1

    if not fixed:
        return svg_text

    print(f"  .. Placeholder sanitizer: demoted {fixed} vectorized placeholder group(s)")
    # Keep default SVG/xlink namespace serialization stable (avoid ns0: prefixes).
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    return ET.tostring(root, encoding="unicode")


def validate_and_autofix_svg(svg_text: str) -> str:
    """对模型生成的 SVG 做一次 XML 合法性校验和简单自修复。

    - 优先尝试直接解析；
    - 解析失败时，用 & 实体修复策略再试一次；
    - 如果仍然失败，则保留原文，后续在 PPTX 编译阶段会跳过该 SVG。
    """
    try:
        ET.fromstring(svg_text)
        return svg_text
    except ET.ParseError:
        try:
            fixed = re.sub(
                r"&(?!(?:amp|lt|gt|quot|apos|#\\d+|#x[0-9a-fA-F]+);)",
                "&amp;",
                svg_text,
            )
            ET.fromstring(fixed)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! 无法修复生成的 SVG XML，将在后续编译阶段被跳过: {exc}")
            return svg_text
        else:
            print("  .. 自动修复生成的 SVG XML & 实体问题")
            return fixed


def image_to_svg(
    api_key: str,
    api_base: str,
    endpoint: str,
    model: str,
    prompt_text: str,
    image_path: Path,
    output_svg_path: Path,
    retries: int,
    timeout: int,
) -> None:
    img_b64 = load_image_as_base64(image_path)

    print(f"[Gemini] {image_path}")
    if _is_google_api_base(api_base):
        body = build_request_body(prompt_text, img_b64)
        text = call_gemini(
            api_key, endpoint, model, body, retries=retries, timeout=timeout
        )
    else:
        text = _call_openai_compat(
            api_base=api_base,
            api_key=api_key,
            model=model,
            prompt_text=prompt_text,
            image_b64=img_b64,
            timeout=timeout,
            retries=retries,
        )
    svg = extract_svg(text)
    svg = sanitize_placeholder_groups(svg)
    svg = validate_and_autofix_svg(svg)

    output_svg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_svg_path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"  -> SVG: {output_svg_path}")


def convert_svg_group_to_pptx(
    svg_dir: Path,
    output_pptx: Path,
    profile: str,
    profile_dir: Optional[Path],
    config_path: Optional[Path],
    refill_placeholders: bool = False,
    refill_mode: str = "source-crop",
    source_image_dir: Optional[Path] = None,
) -> None:
    cmd = [
        sys.executable,
        str(CONVERTER_SCRIPT),
        "--input",
        str(svg_dir),
        "--output",
        str(output_pptx),
        "--profile",
        profile,
    ]
    if profile_dir:
        cmd.extend(["--profile-dir", str(profile_dir)])
    if config_path:
        cmd.extend(["--config", str(config_path)])
    if refill_placeholders:
        cmd.append("--refill-placeholders")
        if refill_mode:
            cmd.extend(["--refill-mode", refill_mode])
        if source_image_dir:
            cmd.extend(["--source-image-dir", str(source_image_dir)])
    print(f"[PPTX] {svg_dir} -> {output_pptx}")
    if config_path:
        print(f"       using compiler config: {config_path}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        if proc.stderr:
            for line in proc.stderr.strip().splitlines():
                print(line, flush=True)
        raise RuntimeError(
            proc.stderr.strip().splitlines()[-1]
            if proc.stderr and proc.stderr.strip()
            else f"Compiler exited with code {proc.returncode}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Batch PNG -> Gemini SVG -> PPTX pipeline"
    )
    parser.add_argument(
        "--input-dir",
        default=str(DEFAULT_INPUT_DIR),
        help=f"PNG 输入目录（默认: {DEFAULT_INPUT_DIR}）",
    )
    parser.add_argument(
        "--output-svg-dir",
        default=str(DEFAULT_OUTPUT_SVG_DIR),
        help=f"SVG 输出目录（默认: {DEFAULT_OUTPUT_SVG_DIR}）",
    )
    parser.add_argument(
        "--output-pptx-dir",
        default=str(DEFAULT_OUTPUT_PPTX_DIR),
        help=f"PPTX 输出目录（默认: {DEFAULT_OUTPUT_PPTX_DIR}）",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="API Key（默认从 --config 或环境变量读取）",
    )
    parser.add_argument(
        "--api-base",
        default="",
        help="API Base（默认从 --config 读取；空时走 Google 原生端点）",
    )
    parser.add_argument(
        "--config",
        default="",
        help=f"运行配置文件（默认: {DEFAULT_RUNTIME_CONFIG_PATH}，若存在）",
    )
    parser.add_argument(
        "--model",
        default=MODEL,
        help=f"Gemini 模型名（默认: {MODEL}）",
    )
    parser.add_argument(
        "--profile",
        choices=SUPPORTED_PROFILES,
        default=DEFAULT_TASK_PROFILE,
        help=f"任务 profile（默认: {DEFAULT_TASK_PROFILE}）",
    )
    parser.add_argument(
        "--profile-dir",
        default="",
        help="profile 配置目录（默认: gemini_pipeline/profiles）",
    )
    parser.add_argument(
        "--skip-pptx",
        action="store_true",
        help="只生成 SVG，不做 PPTX 转换",
    )
    parser.add_argument(
        "--refill-placeholders",
        action="store_true",
        help="在编译 PPTX 时回填 image-placeholder（推荐配合 source-crop）",
    )
    parser.add_argument(
        "--refill-mode",
        choices=("gemini", "source-crop", "auto"),
        default="source-crop",
        help="placeholder 回填模式（默认: source-crop）",
    )
    parser.add_argument(
        "--max-images",
        type=int,
        default=0,
        help="最多处理多少张 PNG（0 表示全部）",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Gemini 请求失败重试次数（默认 3）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="单次 Gemini 请求超时秒数（默认 300，即 5 分钟）",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=8,
        help="并发请求数（默认 8）",
    )
    return parser


def main():
    args = build_parser().parse_args()

    config_path = Path(args.config).expanduser().resolve() if args.config else (
        DEFAULT_RUNTIME_CONFIG_PATH if DEFAULT_RUNTIME_CONFIG_PATH.exists() else None
    )
    runtime_cfg = load_runtime_config(config_path) if config_path else {}
    if config_path and runtime_cfg:
        print(f"Loaded config: {config_path}")

    raw_api_base = (
        args.api_base.strip()
        or str(runtime_cfg.get("DEFAULT_API_BASE") or runtime_cfg.get("base_url") or "").strip()
    )
    use_google = _is_google_api_base(raw_api_base)

    if _cli_flag_present("--model"):
        model_from_cli_or_cfg = args.model
    else:
        model_from_cli_or_cfg = (
            str(runtime_cfg.get("DEFAULT_MODEL") or runtime_cfg.get("chart_model") or "").strip()
            or args.model
        )

    api_key = args.api_key.strip()
    if not api_key:
        if use_google:
            api_key = (
                str(runtime_cfg.get("GEMINI_API_KEY") or "").strip()
                or os.environ.get("GEMINI_API_KEY", "").strip()
                or os.environ.get("OPENAI_API_KEY", "").strip()
            )
        else:
            api_key = (
                str(runtime_cfg.get("OPENAI_API_KEY") or runtime_cfg.get("api_key") or "").strip()
                or os.environ.get("OPENAI_API_KEY", "").strip()
                or os.environ.get("GEMINI_API_KEY", "").strip()
            )
    if not api_key:
        raise RuntimeError("缺少 API_KEY：请通过 --api-key、--config 或环境变量设置")

    profile_dir = Path(args.profile_dir).expanduser().resolve() if args.profile_dir else None
    prompt_text, resolved_model = resolve_profile_prompt_and_model(
        profile=args.profile,
        profile_dir=profile_dir,
        cli_model=model_from_cli_or_cfg,
    )
    endpoint = build_endpoint(resolved_model)
    if resolved_model != model_from_cli_or_cfg:
        print(f"使用 profile={args.profile} 覆盖模型: {resolved_model}")
    provider = "google" if use_google else "openai-compatible"
    print(
        f"Provider={provider}, model={resolved_model}, "
        f"api_base={raw_api_base or '(google-default-endpoint)'}"
    )

    input_dir = Path(args.input_dir).expanduser().resolve()
    output_svg_dir = Path(args.output_svg_dir).expanduser().resolve()
    output_pptx_dir = Path(args.output_pptx_dir).expanduser().resolve()

    if not input_dir.exists():
        raise FileNotFoundError(f"找不到输入目录: {input_dir}")
    if not CONVERTER_SCRIPT.exists() and not args.skip_pptx:
        raise FileNotFoundError(f"找不到转换脚本: {CONVERTER_SCRIPT}")

    png_groups = collect_png_groups(input_dir)
    if not png_groups:
        print(f"未找到 PNG 文件: {input_dir}")
        return

    print(f"发现 {sum(len(v) for v in png_groups.values())} 张 PNG，分布在 {len(png_groups)} 个目录")

    generated_counts = defaultdict(int)
    failed_images = []
    max_images = max(0, args.max_images)
    max_concurrent = max(1, int(args.max_concurrent or 8))

    # Build a single task list for global folder-level concurrency.
    tasks = []
    for rel_dir, image_paths in png_groups.items():
        print(f"\n[Group] {rel_dir if str(rel_dir) else '.'} ({len(image_paths)} 张)")
        for image_path in image_paths:
            rel_image = image_path.relative_to(input_dir)
            output_svg_path = (output_svg_dir / rel_image).with_suffix(".svg")
            tasks.append((rel_dir, image_path, output_svg_path))
            if max_images and len(tasks) >= max_images:
                break
        if max_images and len(tasks) >= max_images:
            break

    if not tasks:
        print("没有可处理的图片任务")
        return

    if max_concurrent <= 1 or len(tasks) <= 1:
        for rel_dir, image_path, output_svg_path in tasks:
            try:
                image_to_svg(
                    api_key,
                    raw_api_base,
                    endpoint,
                    resolved_model,
                    prompt_text,
                    image_path,
                    output_svg_path,
                    retries=args.retries,
                    timeout=args.timeout,
                )
                generated_counts[rel_dir] += 1
            except Exception as exc:  # noqa: BLE001
                failed_images.append((str(image_path), str(exc)))
                print(f"  !! 失败: {image_path} | {exc}")
    else:
        print(f"\n[Batch] 全局并发处理中，线程数={max_concurrent}，任务数={len(tasks)}")
        fut_map = {}
        with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            for rel_dir, image_path, output_svg_path in tasks:
                fut = pool.submit(
                    image_to_svg,
                    api_key,
                    raw_api_base,
                    endpoint,
                    resolved_model,
                    prompt_text,
                    image_path,
                    output_svg_path,
                    args.retries,
                    args.timeout,
                )
                fut_map[fut] = (rel_dir, image_path)
            for fut in as_completed(fut_map):
                rel_dir, image_path = fut_map[fut]
                try:
                    fut.result()
                    generated_counts[rel_dir] += 1
                except Exception as exc:  # noqa: BLE001
                    failed_images.append((str(image_path), str(exc)))
                    print(f"  !! 失败: {image_path} | {exc}")

    generated_groups = {
        rel_dir: (output_svg_dir / rel_dir)
        for rel_dir, ok_count in generated_counts.items()
        if ok_count > 0
    }

    print(f"\nSVG 生成完成：成功目录 {len(generated_groups)}，失败图片 {len(failed_images)}")

    if not args.skip_pptx:
        for rel_dir, svg_dir in generated_groups.items():
            if rel_dir == Path("."):
                output_pptx = output_pptx_dir / f"{input_dir.name}.pptx"
            else:
                output_pptx = output_pptx_dir / rel_dir.parent / f"{rel_dir.name}.pptx"
            convert_svg_group_to_pptx(
                svg_dir,
                output_pptx,
                profile=args.profile,
                profile_dir=profile_dir,
                config_path=config_path,
                refill_placeholders=bool(args.refill_placeholders),
                refill_mode=args.refill_mode,
                source_image_dir=(input_dir / rel_dir),
            )

    if failed_images:
        print("\n失败列表：")
        for image_path, err in failed_images:
            print(f"- {image_path}: {err}")


if __name__ == "__main__":
    main()
