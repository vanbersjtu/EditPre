# Gemini SVG -> PPTX 编译器技术报告

更新时间: 2026-03-06 (v25)
负责人: Codex + xiaoxiaobo

## 1. 目标与范围
本编译器的目标是把 `PNG` 转为结构化 `SVG`，再把 `SVG` 还原为可编辑 `PPTX`，同时尽量保持:
- 布局坐标一致
- 文本可编辑
- 视觉元素可分组拖动
- 图像占位符可回填/重绘

当前主流程文件:
- `gemini_svg_pipeline.py` (PNG -> SVG 调用与清洗)
- `svg_to_pptx_pro.py` (SVG -> PPTX 编译器)

## 2. 总体架构
### 2.1 PNG -> SVG (LLM 生成)
1. 扫描输入目录 PNG
2. 调用 Gemini 文生 SVG 提示词
3. 从响应提取 `<svg>...</svg>`
4. 执行 placeholder 互斥清洗 (避免“同区域既是占位符又是矢量重绘”)
5. 写入输出 SVG

### 2.2 SVG -> PPTX (编译)
1. 解析 SVG 尺寸 (`width/height/viewBox`)
2. 建立坐标换算器 (SVG px -> EMU)
3. 编译普通图元 (`rect/circle/line/polygon/polyline/path/image/text`)
4. 解析 `semantic-layer` 生成文本框
5. 解析 `visual-group` 进行视觉分组
6. 可选: image-placeholder 图像回填 (source-crop / gemini)
7. 输出 PPTX

## 3. SVG 数据约定
### 3.1 图层约定
- `visual-layer`: 视觉图元层
- `semantic-layer`: 文本语义层

### 3.2 分组约定
- `data-type="visual-group"`: 视觉语义组 (文本+装饰+图标等整体拖动)
- `data-type="text-group"`: 文本组
- `data-type="textbox"`: 最小文本框

### 3.3 占位符约定
- `data-type="image-placeholder"` / `data-role="image-placeholder"`
- `data-caption`: 生图提示
- 规则: 同一区域不能既有实质矢量重绘又标记为 placeholder

## 4. 坐标与变换模型
1. 所有元素支持父级 transform 级联解析
2. 使用统一 `TransformMatrix` 处理 translate/scale/rotate/matrix
3. 文本框坐标来自 semantic bbox + transform 后的全局位置
4. slide 物理尺寸由首个 SVG 决定: `Inches(svg_px / dpi)`

## 5. 分组策略 (当前实现)
1. 先收集 visual-group 元素成员
2. 再将 semantic-group 成员挂入对应 visual-group
3. 最后构建 visual-group，保证整体可拖动
4. 独立 semantic-group (未映射 visual-group) 延后构建，避免被背景层遮挡

## 6. 图像占位符回填策略
支持三种模式:
- `gemini`: 直接生图回填
- `source-crop`: 从源 PNG 裁切回填
- `auto`: 先裁切，失败再生图

source-crop 相关能力:
- 背景 alpha 去除
- 与 semantic 文本重叠区域擦除
- 可选“重绘去字”二次生成

## 7. 现状与已修复问题
### 7.1 已修复
1. 文字位置飘移: 统一 semantic bbox + transform 逻辑
2. 文本分组/视觉分组冲突: 避免不必要嵌套 group
3. background 覆盖内容: 修正 visual-group 排序策略，背景强制底层
4. placeholder 与矢量重绘冲突: 增加生成后 sanitizer 自动降级假 placeholder
5. sanitizer 写回命名空间污染: 无改动时保持原文；改动时保留默认 namespace

### 7.2 当前主要技术欠项
1. `python-pptx` FreeformBuilder 仅支持折线段，导致 path/icon 曲线有锯齿感
2. 字体完全一致性仍受本机字体可用性与 Office 渲染差异影响
3. SVG 高级特性 (复杂 filter/mask/blend) 无法 1:1 完整还原

## 8. 下一阶段方案 (方案二)
目标: 提升 icon/path 清晰度，同时保留 visual-group 可拖动。

技术路线:
1. 保留现有分组与文本系统
2. 对 `path` 渲染从“折线采样”升级为“底层 OOXML path 命令写入”
3. 映射 `M/L/C/Q/A/Z` 到 DrawingML 的 path 指令 (优先曲线原语)
4. 对不兼容弧段采用高精度 Bézier 近似，而非低密度折线

预期收益:
- icon/path 边缘更平滑
- 缩放时细节更稳定
- 不影响 visual-group 分组能力

### 8.1 当前进度
- 已完成 v1: `path` 主路径渲染切换为 OOXML 曲线写入
  - `C` -> `a:cubicBezTo`
  - `Q` -> `a:quadBezTo`
  - `A` -> 转为多段 cubic（`as_cubic_curves`）
- 保留兜底链路: OOXML 曲线写入失败时，退回 svgpathtools 折线模式
- 已提高坐标量化精度（EMU 与 local 坐标改为 round，高精度 local units）

## 9. 维护规则 (强制)
从本报告建立后，所有对编译器行为有影响的提交，必须同步更新本报告:
- 变更点
- 影响范围
- 回归风险
- 验证命令与结果

未更新报告的改动视为未完成。

## 10. 变更日志
### 2026-03-05
- 修复 visual-group 层级顺序，避免背景组盖住全页内容
- 独立 semantic-group 延后构建，防止被背景遮挡
- placeholder sanitizer 增加 namespace 稳定写回策略
- 文档首次建立

### 2026-03-05 (方案二 v1)
- `svg_to_pptx_pro.py` 新增 OOXML 曲线路径写入能力（非折线优先）
- 编译输出已验证含 `a:cubicBezTo` / `a:quadBezTo`（曲线原语已入 PPTX）
- 输出文件: `output/pptx/test_3.1_compiled_ooxml_curve_v1.pptx`

### 2026-03-05 (方案二 v1.1)
- 新增描边风格映射:
  - `stroke-linecap` -> OOXML `a:ln@cap`
  - `stroke-linejoin` -> OOXML `a:round/a:bevel/a:miter`
- 目的: 在不改变几何语义的前提下改善边缘观感
- 对比输出: `output/pptx/image_copy6_ooxml_curve_v2_strokejoin.pptx`

### 2026-03-05 (方案二 v1.2)
- 新增简单 SVG 滤镜阴影回放:
  - 解析 `<filter><feDropShadow .../></filter>`
  - 将 `filter=\"url(#id)\"` 映射到形状 `a:outerShdw`
- 目的: 恢复卡片/图标的柔和层次，减少“硬糙感”
- 对比输出: `output/pptx/image_copy3_ooxml_curve_v3_shadow.pptx`

### 2026-03-05 (混合方案 v1: svgBlip + 可编辑文本)
- 新增“复杂 icon 子组 -> svgBlip”后处理链路:
  - 先在版面中放置透明图片壳
  - 保存后注入 `asvg:svgBlip` 关系与 SVG 媒体部件
  - 文本仍走 semantic-layer，可编辑
- 启发式目标: 小尺寸高复杂图标组（避免整卡片替换）
- 默认开启参数: `--hybrid-svgblip-icons`（可 `--no-hybrid-svgblip-icons` 关闭）
- 产物:
  - 单页: `output/pptx/image_copy3_hybrid_svgblip_v2.pptx`
  - 全量: `output/pptx/test_3.1_hybrid_svgblip_v1.pptx`

### 2026-03-05 (工程化拆分 Stage-1)
- 新增 `compiler/` 包骨架:
  - `compiler/legacy_svg_to_pptx_pro.py` (原单体脚本迁移)
  - `compiler/cli.py` (CLI 入口)
  - `compiler/api.py` (稳定调用 API)
  - `compiler/config.py` / `compiler/types.py` / `compiler/constants.py`
- 保留兼容入口:
  - `gemini_pipeline/svg_to_pptx_pro.py` 改为兼容壳，路径与命令不变
  - 旧代码导入方式继续可用（符号重导出）
- 验证:
  - `--help` 正常
  - 单页编译 smoke test 正常

### 2026-03-05 (工程化拆分 Stage-2.1)
- 完成第一批核心模块抽取:
  - `compiler/utils/transforms.py` (`TransformMatrix`, `parse_transform`)
  - `compiler/pptx/coordinates.py` (`CoordinateConverter`)
  - `compiler/constants.py` (核心编译常量集中管理)
- `legacy_svg_to_pptx_pro.py` 已切换引用新模块实现，删除对应内联大段定义
- 兼容性验证:
  - `svg_to_pptx_pro.py --help` 正常
  - 单页编译 smoke test 正常 (`_smoke_stage2_refactor.pptx`)

### 2026-03-05 (工程化拆分 Stage-2.2)
- 完成第二批工具模块抽取:
  - `compiler/utils/svg_helpers.py` (`tag_name`, `natural_sort_key`)
  - `compiler/utils/lengths.py` (`parse_length`, `parse_opacity`, `normalize_rotation`)
  - `compiler/utils/colors.py` (`parse_color`, `NAMED_COLORS`)
  - `compiler/utils/text.py` (`CJK_RE`, `GENERIC_FONTS`, `has_cjk`)
  - `compiler/constants.py` 新增 `SUPPORTED_IMAGE_ASPECT_RATIOS`
- `legacy_svg_to_pptx_pro.py` 删除同名内联实现并改为导入新模块
- 兼容性验证:
  - 全量目录编译正常 (`test_3.1_hybrid_svgblip_stage2_2.pptx`)

### 2026-03-05 (工程化拆分 Stage-2.3)
- 新增:
  - `compiler/utils/style_context.py`（`StyleContext` + inline style 解析）
  - `compiler/utils/effects.py`（`feDropShadow` 提取与 OOXML 阴影应用）
- `legacy_svg_to_pptx_pro.py` 改为薄封装委托:
  - `extract_simple_drop_shadow_filters` -> `utils.effects`
  - `apply_svg_filter_shadow_if_needed` -> `utils.effects`
  - 删除内联 `StyleContext` 大块定义，统一引用新模块
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_stage2_3_refactor.pptx`
  - 全量 test_3.1: `output/pptx/test_3.1_hybrid_svgblip_stage2_3_refactor.pptx`

### 2026-03-05 (工程化拆分 Stage-2.4)
- 新增:
  - `compiler/pptx/path_renderer.py`（`add_svg_path` 全链路拆分，含 OOXML 曲线写入/采样回退/简易解析回退）
- `legacy_svg_to_pptx_pro.py` 调整:
  - `add_svg_path` 改为委托 `path_renderer.add_svg_path`
  - 删除 legacy 内联 path 渲染大段实现（`_collect_path_draw_ops` / `_write_ops_to_shape_custgeom` / `_add_svg_path_*`）
  - 删除 legacy 内联 svgpathtools 依赖导入与对应采样常量依赖
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_stage2_4_path_refactor.pptx`
  - 全量 test_3.1: `output/pptx/test_3.1_hybrid_svgblip_stage2_4_path_refactor.pptx`

### 2026-03-05 (工程化拆分 Stage-2.5)
- 新增:
  - `compiler/pptx/text_style.py`（字体主题判断、字体选取、EA 字体注入、字号换算、run 样式应用）
- `legacy_svg_to_pptx_pro.py` 调整:
  - 删除内联文本样式函数块（`font_family_is_theme` / `pick_font_name` / `set_run_ea_font` / `set_run_font_size_from_px` / `apply_text_run_style`）
  - 改为导入 `pptx/text_style.py` 提供同名实现
  - 保留 `GENERIC_FONTS` 常量导入，修复 `add_svg_text` 直接引用场景
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_stage2_5_text_refactor.pptx`
  - 全量 test_3.1: `output/pptx/test_3.1_hybrid_svgblip_stage2_5_text_refactor.pptx`

### 2026-03-05 (工程化拆分 Stage-2.6)
- 新增:
  - `compiler/pptx/semantic_text.py`（semantic-layer 文本提取、textbox 重建、absolute 文本回放）
- `legacy_svg_to_pptx_pro.py` 调整:
  - `extract_semantic_textboxes` 改为委托 `semantic_text.extract_semantic_textboxes`
  - `add_semantic_textbox` 改为委托 `semantic_text.add_semantic_textbox`
  - `add_semantic_text_items_absolute` 改为委托 `semantic_text.add_semantic_text_items_absolute`
  - 删除 legacy 内联的 semantic 主链大段实现（迁移到独立模块）
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_stage2_6_semantic_refactor.pptx`
  - 全量 test_3.1: `output/pptx/test_3.1_hybrid_svgblip_stage2_6_semantic_refactor.pptx`

### 2026-03-05 (工程化拆分 Stage-2.7)
- 新增:
  - `compiler/pptx/image_placeholder.py`（placeholder 提取、clip/transform 解析、非矩形 clip 栅格化、占位图回填插入）
- `legacy_svg_to_pptx_pro.py` 调整:
  - `parse_matrix_simple / parse_scale_simple / apply_transform_chain / clip_path_is_rect / classify_image_placeholder_group / extract_image_placeholders / add_image_placeholder` 改为委托新模块
  - 保留同名函数壳，兼容历史导入调用路径
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_stage2_7_placeholder_refactor.pptx`
  - 全量 test_3.1: `output/pptx/test_3.1_hybrid_svgblip_stage2_7_placeholder_refactor.pptx`

### 2026-03-05 (工程化拆分 Stage-2.8)
- 新增:
  - `compiler/pptx/image_refill.py`（Gemini 生图、source-crop、重绘去字、源图解析、背景 alpha 处理）
- `legacy_svg_to_pptx_pro.py` 调整:
  - `parse_aspect_ratio / pick_supported_aspect_ratio / rect_intersection_area / _iter_genai_parts / generate_placeholder_image / guess_image_mime / redraw_placeholder_crop_without_text / resolve_source_image_for_svg / estimate_border_bg_color / apply_background_alpha / crop_placeholder_from_source` 改为委托新模块
  - 保留同名函数壳，兼容历史导入路径与调用语义
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_stage2_8_refill_refactor.pptx`
  - 全量 test_3.1: `output/pptx/test_3.1_hybrid_svgblip_stage2_8_refill_refactor.pptx`

### 2026-03-05 (工程化拆分 Stage-2.9)
- 新增:
  - `compiler/pptx/chart_codegen.py`（图表代码提示词、LLM 代码生成、安全执行）
- `legacy_svg_to_pptx_pro.py` 调整:
  - `CHART_CODE_PROMPT / generate_chart_code / execute_chart_code` 改为委托新模块
  - 保留同名常量与函数壳，兼容历史导入与调用路径
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_stage2_9_chart_codegen_refactor.pptx`

### 2026-03-06 (v21: rembg 接入编译主流程)
- 变更点:
  - `compiler/pptx/image_refill.py` 新增 rembg 后处理主链路:
    - 生图回填阶段支持 `remove_bg=True` 时优先 rembg 抠图
    - source-crop 阶段支持 `source_crop_use_rembg` 优先 rembg，再回退旧阈值法
    - 新增 alpha 覆盖率有效性校验，避免“全透/全不透”异常结果
  - `compiler/legacy_svg_to_pptx_pro.py`:
    - `generate_placeholder_image(...)` 包装器新增 `remove_bg` 透传
    - 两个 placeholder 生图调用点补齐 `remove_bg` 参数透传
    - CLI 新增 rembg 相关配置:
      - `--source-crop-use-rembg/--no-source-crop-use-rembg`
      - `--source-crop-rembg-min-nonopaque-ratio`
      - `--source-crop-rembg-max-nonopaque-ratio`
      - `--source-crop-require-rembg`
      - `--remove-bg-require-rembg`
- 影响范围:
  - 仅影响 image-placeholder 回填流程（生图回填 + source-crop 去背景）
  - 不影响普通 SVG 几何编译、文本重建、visual-group 分组逻辑
- 回归风险:
  - rembg 引入新依赖，对本地 Python 环境兼容性敏感（尤其 `numpy/onnxruntime` 组合）
  - 低纹理或高纯色图像可能触发 alpha 比例校验失败，进入 fallback 分支
- 验证命令与结果:
  - `python -m py_compile gemini_pipeline/compiler/legacy_svg_to_pptx_pro.py gemini_pipeline/compiler/pptx/image_refill.py gemini_pipeline/svg_to_pptx_pro.py`
  - 结果: 通过（无语法错误）

### 2026-03-06 (v22: chart codegen 默认切换到 cdn.12ai.org 链路)
- 变更点:
  - `compiler/pptx/chart_codegen.py`:
    - API key 选择策略升级:
      - OpenAI-compatible 网关（默认 `https://cdn.12ai.org/v1`）优先且仅默认读取 `OPENAI_API_KEY`
      - Google 端点才默认优先读取 `GEMINI_API_KEY`
    - 避免在 cdn 网关下误用 `GEMINI_API_KEY` 导致 401
  - `compiler/io/config_loader.py`:
    - 配置键归一化:
      - `DEFAULT_API_BASE -> base_url`
      - `OPENAI_API_KEY -> api_key`
      - `DEFAULT_MODEL -> chart_model`
  - `compiler/legacy_svg_to_pptx_pro.py`:
    - chart 默认配置兜底:
      - `base_url` 默认 `https://cdn.12ai.org/v1`
      - `chart_model` 默认 `gemini-3.1-pro-preview`
- 影响范围:
  - 仅影响图表代码生成（chart placeholder -> python-pptx native chart）
  - 不影响普通图元编译、文本重建、image-placeholder 回填链路
- 回归风险:
  - 若仅设置 `GEMINI_API_KEY` 且使用 cdn 网关，chart codegen 将提示未配置 key（这是预期的显式失败）
- 验证命令与结果:
  - `python -m py_compile gemini_pipeline/compiler/pptx/chart_codegen.py gemini_pipeline/compiler/io/config_loader.py gemini_pipeline/compiler/legacy_svg_to_pptx_pro.py`
  - `env -u OPENAI_API_KEY -u GEMINI_API_KEY python gemini_pipeline/svg_to_pptx_pro.py --input gemini_pipeline/output/svg/test_3.1/testbanana_v19/slide_0012.svg --output gemini_pipeline/output/pptx/_smoke_chart_cdn_keyroute.pptx --semantic-mode textbox --chart-fallback-image`
  - 结果: 通过；日志从原先 401 变为明确的 “API key not set”。

### 2026-03-06 (v23: 默认本地 config 自动加载)
- 变更点:
  - `compiler/legacy_svg_to_pptx_pro.py`:
    - 新增默认配置路径常量:
      - `gemini_pipeline/config/runtime_api_config.json`
    - 当未传 `--config` 且默认文件存在时，自动加载该 config
    - `--config` help 文案同步更新默认自动加载行为
  - 新增默认运行时配置文件:
    - `gemini_pipeline/config/runtime_api_config.json`
    - 内含:
      - `DEFAULT_API_BASE`
      - `DEFAULT_MODEL`
      - `OPENAI_API_KEY`
      - chart 相关默认参数
- 影响范围:
  - 图表代码生成命令可免去每次手工传 key/base/model
  - 不影响手工传 `--config` 覆盖行为
- 回归风险:
  - 本地配置文件包含敏感 key，需要注意文件权限和仓库提交策略
- 验证命令与结果:
  - `python -m py_compile gemini_pipeline/compiler/legacy_svg_to_pptx_pro.py`
  - 后续 smoke 见本轮执行日志（未显式传 `--config` 时成功提示加载默认配置文件）

### 2026-03-06 (v24: image-placeholder 生图也统一走 runtime config)
- 变更点:
  - `compiler/pptx/image_refill.py`:
    - 新增 OpenAI-compatible 生图链路（`/chat/completions`），支持:
      - 文本生图（placeholder caption）
      - 图生图重绘（source-crop redraw，附带 seed image）
    - 自动根据 `api_base` 判定:
      - Google 端点 -> `google-genai SDK`
      - 其他端点（如 `cdn.12ai.org`）-> OpenAI-compatible HTTP 调用
  - `compiler/legacy_svg_to_pptx_pro.py`:
    - `--image-api-key` 默认改为 `GEMINI_API_KEY || OPENAI_API_KEY`
    - 新增 `--image-api-base` 参数
    - image refill 配置支持从 config 自动继承:
      - `image_api_key / api_key`
      - `image_api_base / base_url`
      - `image_model`
    - 日志新增 `api_base=` 显示当前生图链路
  - `compiler/io/config_loader.py`:
    - 新增映射:
      - `IMAGE_API_BASE -> image_api_base`
      - `IMAGE_MODEL -> image_model`
      - `OPENAI_API_KEY/GEMINI_API_KEY -> image_api_key`
  - `config/runtime_api_config.json`:
    - 新增 `IMAGE_API_BASE` 与 `IMAGE_MODEL`
- 影响范围:
  - image-placeholder 的生图/重绘链路现在可和 chart codegen 一样统一吃 runtime config
  - 默认无需每次命令行重复传 image key/base
- 回归风险:
  - OpenAI-compatible 网关响应格式可能存在差异，已做 data-uri 与 `b64_json` 双通道解析
- 验证命令与结果:
  - `env -u OPENAI_API_KEY -u GEMINI_API_KEY python gemini_pipeline/svg_to_pptx_pro.py --input gemini_pipeline/output/svg/test_3.1/testbanana/slide_0002.svg --output gemini_pipeline/output/pptx/_smoke_image_config_v24.pptx --semantic-mode textbox --refill-placeholders --chart-fallback-image`
  - 结果: 成功，日志显示 `api_base=https://cdn.12ai.org/v1` 且 `2 generated`。

### 2026-03-06 (v25: 生图 key 优先级修复 + 并发 8 预取)
- 变更点:
  - `compiler/legacy_svg_to_pptx_pro.py`:
    - 修复 image refill key 优先级，避免环境变量 `GEMINI_API_KEY` 覆盖 runtime config 的 `OPENAI_API_KEY` 导致 401
    - 新增参数 `--image-max-concurrent`（默认 8）
    - 在 `convert_svg_to_slide` 中新增 placeholder 生图预取线程池（gemini 模式），并发调用 API 先写缓存再顺序编译
  - 并发策略:
    - 每页按 placeholder 预取 (`ThreadPoolExecutor`)
    - 命中预取结果直接回填，减少串行等待
- 影响范围:
  - image-placeholder 生图吞吐与稳定性提升
  - 不影响普通图元、文本、图表 native 渲染
- 回归风险:
  - 并发提高会增加瞬时 API 压力，可能触发平台侧限流
  - 预取阶段会对最终未使用的 placeholder 产生少量“冗余生成”缓存
- 验证命令与结果:
  - 单页并发 smoke:
    - `python gemini_pipeline/svg_to_pptx_pro.py --input /Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline/output/svg/test_3.1/testbanana/slide_0002.svg --output /Users/xiaoxiaobo/Downloads/sjtuwenber/gemini_pipeline/output/pptx/_smoke_prefetch8_v25.pptx --semantic-mode textbox --refill-placeholders --chart-fallback-image --image-max-concurrent 8`
    - 结果: 日志出现 `prefetching 2 placeholder images (concurrency=8)`，`2 generated`
  - 全量:
    - `... --input .../testbanana --output .../testbanana_full_gemini_refill_v25_conc8.pptx --refill-placeholders --image-max-concurrent 8`
    - 结果: 成功生成，14 张回填图缓存，产物 28MB。
  - 全量 test_3.1: `output/pptx/test_3.1_hybrid_svgblip_stage2_9_chart_codegen_refactor.pptx`

### 2026-03-05 (工程化拆分 Stage-2.10)
- 新增:
  - `compiler/io/config_loader.py`（`config.json` 与 `image_placeholders` 映射加载）
  - `compiler/io/__init__.py`
- `legacy_svg_to_pptx_pro.py` 调整:
  - `load_config / load_placeholders` 改为委托 `io/config_loader.py`
  - 保留同名函数壳，兼容历史导入路径
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_stage2_10_config_loader_refactor.pptx`
  - 全量 test_3.1: `output/pptx/test_3.1_hybrid_svgblip_stage2_10_config_loader_refactor.pptx`

### 2026-03-05 (工程化拆分 Stage-2.11)
- 新增:
  - `compiler/pptx/svg_blip.py`（复杂图标 svgBlip 启发式、区域 SVG 切片、临时壳图插入、PPTX svgBlip 注入）
- `legacy_svg_to_pptx_pro.py` 调整:
  - `_elem_is_hidden_bbox_rect / _group_local_bbox_from_hidden_rect / _transform_bbox / _count_group_graphics`
  - `should_render_group_as_svgblip / build_svg_region_snippet / add_svgblip_region_picture / inject_svg_blips_into_pptx`
  - 以上函数改为委托新模块，保留 legacy 函数壳兼容调用
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_stage2_11_svgblip_refactor.pptx`
  - 全量 test_3.1: `output/pptx/test_3.1_hybrid_svgblip_stage2_11_svgblip_refactor.pptx`

### 2026-03-05 (工程化拆分 Stage-2.12)
- 新增:
  - `compiler/pptx/grouping.py`（`extract_visual_group_meta`）
- `legacy_svg_to_pptx_pro.py` 调整:
  - `extract_visual_group_meta` 改为委托 `pptx/grouping.py`
  - 保留同名函数壳，兼容历史导入路径
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_stage2_12_grouping_refactor.pptx`
  - 全量 test_3.1: `output/pptx/test_3.1_hybrid_svgblip_stage2_12_grouping_refactor.pptx`

### 2026-03-05 (工程化拆分 Stage-2.13)
- 新增:
  - `compiler/pptx/shape_style.py`（`apply_fill_to_shape / apply_stroke_to_shape`）
- `legacy_svg_to_pptx_pro.py` 调整:
  - `apply_fill_to_shape / apply_stroke_to_shape` 改为委托 `pptx/shape_style.py`
  - 保留同名函数壳，兼容历史导入路径
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_stage2_13_shape_style_refactor.pptx`
  - 全量 test_3.1: `output/pptx/test_3.1_hybrid_svgblip_stage2_13_shape_style_refactor.pptx`

### 2026-03-05 (工程化拆分 Stage-2.14)
- 新增:
  - `compiler/utils/transform_extract.py`（`parse_transform_rotation / parse_transform_xy`）
- `legacy_svg_to_pptx_pro.py` 调整:
  - `parse_transform_rotation / parse_transform_xy` 改为委托 `utils/transform_extract.py`
  - 清理 legacy 中对应内联解析实现（保留同名函数壳）
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_stage2_14_transform_extract_refactor.pptx`
  - 全量 test_3.1: `output/pptx/test_3.1_hybrid_svgblip_stage2_14_transform_extract_refactor.pptx`

### 2026-03-05 (工程化拆分 Stage-2.15)
- 新增:
  - `compiler/pptx/text_helpers.py`（`read_text_content / group_text_lines / should_insert_space / assemble_line_text / estimate_text_width_px / baseline_to_top_offset_px`）
- `legacy_svg_to_pptx_pro.py` 调整:
  - 上述文本辅助函数改为委托 `pptx/text_helpers.py`
  - 移除 legacy 中对应内联实现，保留同名函数壳兼容历史导入
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_stage2_15_text_helpers_refactor.pptx`
  - 全量 test_3.1: `output/pptx/test_3.1_hybrid_svgblip_stage2_15_text_helpers_refactor.pptx`

### 2026-03-05 (行为增强 v16: placeholder 级 RGBA 开关)
- `gemini_svg_pipeline.py` 提示词增强:
  - 新增 `data-remove-bg=\"true|false\"` 约束，要求模型在 `image-placeholder` 上显式判断是否需要 RGBA 去背景。
  - 语义: `true` 用于不规则主体贴合背景；`false` 用于完整矩形照片/示意图，避免错误抠图。
- `compiler/pptx/image_placeholder.py`:
  - 新增 `parse_placeholder_remove_bg`，支持解析 `data-remove-bg`（兼容 `data-rgba` / `data-needs-rgba`）。
  - placeholder 元数据新增 `remove_bg` 字段并透传到回填流程。
- `compiler/pptx/image_refill.py`:
  - `crop_placeholder_from_source` 支持 per-placeholder 覆盖 `source_crop_remove_bg` 全局开关。
  - 优先级: placeholder 字段 > entry 覆盖 > 全局配置。
- `compiler/legacy_svg_to_pptx_pro.py`:
  - source-crop 调用传入 `ph_for_insert`，保证占位符级字段可生效。
- `gemini_svg_pipeline.py` sanitizer:
  - 伪 placeholder 降级时同步清理 `data-remove-bg / data-rgba / data-needs-rgba`。
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_remove_bg_flag.pptx`

### 2026-03-06 (行为增强 v17: 图表占位符强策略)
- `gemini_svg_pipeline.py` 提示词增强:
  - 图表主体强制仅输出 `image-placeholder`（禁止图表 path/rect 数据图元细节）。
  - 允许保留图表外围装饰矢量（卡片背景/边框/阴影等）。
  - 新增 `data-chart-spec` 约束（`data-is-chart=true` 时必须提供结构化图表规格）。
- `compiler/pptx/image_placeholder.py`:
  - 新增 `data-chart-spec` 解析并透传到 placeholder 元数据。
- `compiler/pptx/chart_codegen.py`:
  - `generate_chart_code` 新增 `chart_spec` 参数，提示词中明确“chart_spec 优先于 caption”。
- `compiler/legacy_svg_to_pptx_pro.py`:
  - `is_chart=true` 时默认优先原生图表渲染；
  - 图表失败后默认不再回退图像（可通过 `--chart-fallback-image` 显式开启）。
  - 新增 CLI 参数 `--chart-fallback-image / --no-chart-fallback-image`。
- 兼容性验证:
  - 单页 smoke: `output/pptx/_smoke_chart_placeholder_policy_v17.pptx`

### 2026-03-06 (待办探索 v18: 分层重生图流水线，暂缓)
- 背景:
  - 用户提出“模型先输出完整图层与文本结构，再由生图模型按层依次生成素材并回填”的方案，以减少局部擦字和错位问题。
- 可行性结论:
  - 技术上可实现，但当前阶段不建议直接并入主流程；先作为实验分支探索。
- 主要风险:
  - 多次生图链路会放大风格漂移与随机性，跨层对齐误差会累积。
  - 成本与时延显著上升（多轮推理 + 回填 + 重排）。
  - 可编辑文本与图像内文字边界更复杂，回归难度大。
- 建议的实验边界:
  - 仅对“强绑定信息图”（地图标注、复杂 callout 图）启用。
  - 主流程仍保持“结构化 SVG + 原生图表 + 选择性 source-crop”。
- 后续触发条件（满足再推进）:
  - 建立固定样例集并定义可量化验收指标（重影率/错位率/人工修改时长）。
  - 单页实验稳定通过后，再考虑扩大到批量流程。

### 2026-03-06 (行为增强 v19: raster 文本策略收敛)
- 目标:
  - 解决“图内标签文字”和 semantic-layer 文字重复叠加导致的错位/重影。
  - 明确图表与强绑定信息图的输出边界，减少模型混合输出（同区既矢量又占位符）。
- `gemini_svg_pipeline.py` 提示词更新:
  - 移除“图表元素优先矢量绘制”的歧义描述，统一为“图表主体必须走 image-placeholder”。
  - 强化 `data-text-policy="raster"` 语义:
    - 适用于地图标注/信息图 callout/图内注释等强绑定文本；
    - 该区域文本不应再进入 semantic-layer 重复输出。
- `prompt.md` 同步更新:
  - 与主提示词保持一致的图表硬规则、图文强绑定规则、互斥规则。
- `compiler/legacy_svg_to_pptx_pro.py`:
  - 新增 raster visual-group 抑制路径:
    - 若 placeholder 显式 `data-text-policy="raster"` 且挂到某 `visual-group`，
      则该组内 semantic 文本在编译时直接抑制，不再靠纯面积重叠阈值猜测。
  - 保留原有面积重叠抑制作为兜底（用于缺少 visual-group 映射的情况）。
- `past/.../batch_convert_openai.py`:
  - 新增 `sanitize_placeholder_groups` 后处理，自动降级“伪占位符”（已矢量化却仍标占位）的输出。
- 结论:
  - 该版本不对旧 SVG 做自动推断；依赖新提示词重新生成 SVG，再由编译器按显式策略执行。

### 2026-03-06 (行为增强 v20: 占位符回填自适应策略)
- 背景问题:
  - source-crop + RGBA 在大面积区域（如地图底图）会出现“过度透明/发灰”。
  - 角落装饰类 placeholder 容易把附近标题文字裁进图片，视觉污染明显。
- 本次策略:
  - 对 `remove_bg=true` 的 placeholder，默认优先生图回填（generation-first），失败再回退 source-crop。
  - 对大面积 placeholder 自动禁用 RGBA 去背景，避免整块被过度抠图。
  - `remove_bg=true` 时使用更严格的重叠文本擦除阈值，优先清掉边界交叠文字。
- 代码变更:
  - `compiler/legacy_svg_to_pptx_pro.py`
    - 新增 generation-first 逻辑（`prefer_generate_remove_bg`）。
    - 新增 CLI:
      - `--prefer-generate-remove-bg / --no-prefer-generate-remove-bg`
      - `--source-crop-max-remove-bg-area-ratio`
      - `--source-crop-text-erase-overlap-threshold-remove-bg`
  - `compiler/pptx/image_refill.py`
    - `crop_placeholder_from_source` 内加入:
      - 大面积 RGBA 自动禁用判断；
      - `remove_bg=true` 场景的更激进文本擦除阈值；
      - 交叠判定改为按较小区域归一化，改善角落部分交叠文字漏擦。

### 2026-03-06 (行为增强 v28: 去背景伪影抑制实验落地)
- 背景问题:
  - `rembg` 在部分装饰图（叶子、水彩角标、底图）上产生半透明灰雾/黑边伪影。
  - 导致回填后与页面背景融合不自然，边缘视觉脏。
- 方案:
  - `remove_bg=true` 的生图提示词新增“纯白背景、无阴影/烟雾/纹理、无文字”约束，提升后处理可分离性。
  - 去背景模式增强:
    - `auto` 词典加入 `watercolor/leaf/decorative/clipart/ornament/corner/isometric/diagram` 等关键词，更多装饰图自动走 `flat`。
    - `photo`（rembg）结果新增后处理硬化：当半透明比例过高时，自动执行 alpha 二值硬化，压掉灰雾边。
- 代码变更:
  - `compiler/pptx/image_refill.py`
    - 新增 `_harden_alpha_edges`、`_maybe_harden_rembg_edges`。
    - `remove_bg=true` 时拼接 `Background constraints` 提示词。
    - `photo` 与 `photo-fallback` 路径都接入半透明边缘硬化。
  - `compiler/legacy_svg_to_pptx_pro.py`
    - 增加配置透传:
      - `rembg_post_hard_enabled`
      - `rembg_post_hard_trigger_semi_ratio`
      - `rembg_post_hard_threshold`
      - `rembg_post_hard_blur`
      - `remove_bg_style_prompt`
  - `config/runtime_api_config.json`
    - 写入上述新默认参数，后续全量编译默认生效。
- 验证结论:
  - `slide_0017`（四角水彩图）新跑结果 `semi_ratio=0`（4/4），边缘灰雾显著下降。
  - `slide_0016`（地图）新跑结果透明边缘稳定，未见此前的大面积半透明伪影。

### 2026-03-06 (行为修复 v28b: 相对缓存路径图片未嵌入 PPTX)
- 问题:
  - 当 `--image-cache-dir` 使用相对路径时，`add_image_placeholder` 将生成图路径错误地按 `svg_path.parent` 拼接，
    导致“图片已生成但未插入 PPTX”（`ppt/media` 为空）。
- 修复:
  - `compiler/pptx/image_placeholder.py`
    - 对 `entry.image_path`（回填生成图）优先按 `cwd` 解析；
    - 同时保留 SVG 相对路径解析作为次级回退；
    - 支持多候选路径依次探测，命中即插图。
- 验证:
  - 单页 `slide_0016`：编译日志恢复 `images/generated` 计数，`ppt/media=4`。
  - 全量 `testbanana_v19`：`ppt/media=19`，图片回填正常。

### 2026-03-06 (行为增强 v29: remove_bg 分模式背景色策略)
- 问题复盘:
  - `remove_bg=true` + 白底时，若前景本身包含白色结构（如等轴图白色层），会被误抠成缺口。
- 策略升级:
  - `flat` 模式（图标/示意图/等轴图/装饰）默认改为绿幕底图生成，再执行色键抠图。
  - `photo` 模式继续白底生成 + rembg。
- 实现:
  - `compiler/pptx/image_refill.py`
    - 在 `generate_placeholder_image` 中按 `resolved_remove_bg_mode` 选择背景约束提示词:
      - `remove_bg_style_prompt_flat`
      - `remove_bg_style_prompt_photo`
      - 若 `remove_bg_style_prompt` 非空则作为全局覆盖。
  - `compiler/legacy_svg_to_pptx_pro.py`
    - 将新配置字段透传到 `image_refill_config`。
  - `config/runtime_api_config.json`
    - 新增并启用:
      - `remove_bg_style_prompt_flat`（默认 #00FF00 纯色背景）
      - `remove_bg_style_prompt_photo`（默认白底）
    - 将 `remove_bg_style_prompt` 置空，避免覆盖分模式策略。

### 2026-03-06 (行为增强 v30: 绿幕抠图精修链路)
- 触发背景:
  - v29 绿底方案缓解了“白色主体被误抠”，但仍存在局部绿点噪声与边缘溢绿。
- 新增策略（flat 模式）:
  - HSV 绿幕分割替代单纯 RGB 距离阈值:
    - Hue/Saturation/Value + 绿色优势联合判定。
  - 小噪点清理:
    - 对 alpha>0 的小连通域进行“绿色占比”筛除，清除残留绿点。
  - 边缘去溢色（despill）:
    - 对半透明边缘像素压制绿色通道，减少绿边污染。
- 代码变更:
  - `compiler/pptx/image_refill.py`
    - 新增:
      - `_apply_green_chroma_alpha`
      - `_remove_small_green_islands`
      - `_despill_green_edges`
    - `flat` remove-bg 主流程接入以上三步。
  - `compiler/legacy_svg_to_pptx_pro.py`
    - 透传新增参数到 `image_refill_config`。
  - `config/runtime_api_config.json`
    - 增加可调阈值:
      - `flat_chroma_*`
      - `flat_remove_small_green_islands`, `flat_green_island_*`
      - `flat_despill_*`

### 2026-03-06 (路线规划 v31: 扩展对象类型 Poster / Figure)
- 目标:
  - 在“普通 slide”稳定后，将流水线扩展到更复杂视觉对象:
    - `poster`（海报/单页视觉主导版面）
    - `figure`（论文图/技术插图/多子图组合）
  - 维持当前优势: 可编辑文本、可分组拖动、图像占位可回填。

- 对象定义与协议扩展:
  - `data-object-type` 新增枚举: `slide | poster | figure`。
  - `poster`:
    - 强调大幅背景图、标题区、品牌元素、装饰层次；
    - 支持“主题视觉锁定区”（文本不重排）。
  - `figure`:
    - 支持多子图布局（A/B/C...），统一图例、坐标轴、注释层；
    - 图表区继续遵守“图表主体只用 image-placeholder + chart-spec”。

- SVG 生成阶段（提示词）改造点:
  - 新增对象级规则块（按 `data-object-type` 分支）:
    - `poster`: 强化视觉主次、对齐栅格、背景层与文本层解耦。
    - `figure`: 强化子图边界、共享图例、轴标签语义完整性。
  - 新增字段约束:
    - `data-panel-id`（figure 子图编号）
    - `data-locked-layout`（禁止自动重排区域）
    - `data-asset-priority`（关键视觉元素优先级）

- 编译器阶段改造点:
  - 分组:
    - 以 `panel-id` + `visual-group` 构建稳定组，保证整块拖动一致。
  - 文本:
    - `poster` 默认更保守的重排（优先 absolute/locked 模式）。
    - `figure` 对轴标签、图例标签强制落在可编辑文本层。
  - 图像回填:
    - `poster` 背景图默认 `remove_bg=false`。
    - `figure` 中图标/装饰按现有 `flat/photo` 自适应去背景。

- 里程碑（建议顺序）:
  - M1: 协议与提示词
    - 完成 `data-object-type/panel-id/locked-layout` 协议定义与提示词更新。
  - M2: 编译器解析与分组
    - 支持对象级分组策略与锁定布局策略。
  - M3: 回填策略分流
    - poster/figure 的占位回填默认策略分离并可配置。
  - M4: 质量评估
    - 建立对象级验收集（poster 20 页, figure 20 页）并输出指标报表。

- 验收指标:
  - 位置一致性: 关键元素偏移 <= 2%（相对框）。
  - 文本可编辑率: >= 95%。
  - 视觉噪点率: 去背景伪影页占比 <= 5%。
  - 人工修订时长: 较当前流程下降 >= 30%。

### 2026-03-06 (实施启动 v31a: Profile 化骨架接入)
- 目标:
  - 以“单内核 + 多 profile 配置”替代复制多套 pipeline/compiler。
  - 保持默认 `slide` 行为稳定，同时开放 `figure/poster` 分任务入口。
- 新增:
  - `compiler/io/profile_loader.py`
    - `DEFAULT_PROFILE / SUPPORTED_PROFILES`
    - `load_profile_spec`
    - `apply_profile_overrides`（深度合并）
    - `resolve_profile_prompt_file`
  - `profiles/slide.json`
  - `profiles/figure.json`
  - `profiles/poster.json`
- Pipeline 变更:
  - `gemini_svg_pipeline.py`
    - 新增 CLI:
      - `--profile {slide,figure,poster}`
      - `--profile-dir`
    - 支持按 profile 注入 prompt suffix（后续可扩展为完整 prompt 文件）。
    - 调用 compiler 时透传 `--profile/--profile-dir`，保证前后端一致。
- Compiler 变更:
  - `compiler/legacy_svg_to_pptx_pro.py`
    - 新增 CLI:
      - `--profile {slide,figure,poster}`
      - `--profile-dir`
    - 运行时将 profile 的 `compiler` 配置合并进 `chart_config`。
    - 支持 `compiler_cli` 的默认参数覆盖（仅在未显式传 CLI flag 时生效）。
- 回归验证:
  - `--profile figure` 单页 smoke:
    - `output/pptx/_smoke_profile_figure_v31.pptx`
  - `--profile slide` 单页 smoke:
    - `output/pptx/_smoke_profile_slide_v31.pptx`

### 2026-03-06 (v31b: 整文件夹全局并发请求池)
- 目标:
  - 将 PNG->SVG 请求从“按子目录并发”升级为“整文件夹统一线程池并发”。
  - 满足批量目录处理时稳定并发 `8` 的默认需求。
- Pipeline 变更:
  - `gemini_svg_pipeline.py`
    - 维持 CLI 参数 `--max-concurrent`（默认 `8`）。
    - 将任务构建为全局 `tasks` 列表，在一次 `ThreadPoolExecutor` 中统一调度。
    - 保留按组日志输出与按组 PPTX 汇总逻辑（`generated_counts` -> `generated_groups`）。
- 影响:
  - 多子目录输入下，总吞吐提升，避免“目录切换导致的并发空洞”。
  - `--max-images` 仍按全局任务数截断，行为可预测。

## Branch Split Note (2026-03-08)
- `slide-stable` now contains the misplaced slide/core working-tree changes that were made on `codex/physui` by accident.
- Included in this split commit: compiler config pass-through, runtime config normalization, chart gateway key routing, slide prompt protocol updates, and legacy batch config support.

## Slide App Packaging (2026-03-09)

Problem:
- The slide pipeline was only usable through CLI entrypoints.
- For lab use, the workflow needed an application layer: upload PDF, render each page to PNG, run the existing slide pipeline, preview generated SVG pages, and download the final PPTX.
- Deploying the full long-running Python pipeline directly inside Vercel serverless functions is not a good fit because the job is multi-step, file-heavy, and can exceed serverless runtime expectations.

Solution:
- Added a two-tier app wrapper around the stable slide pipeline:
  - `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-api/main.py`
    - FastAPI backend
    - Accepts PDF uploads
    - Renders PDF pages to PNG via PyMuPDF
    - Calls `gemini_pipeline/gemini_svg_pipeline.py --profile slide`
    - Exposes job status, SVG preview URLs, and PPTX download
  - `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-web/`
    - Static frontend intended for Vercel deployment
    - Upload form, polling UI, per-page SVG preview, PPTX download link
- Added runtime job workspace at `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/gemini_pipeline/app_data/jobs/`.

Validation:
- Backend module syntax check passed with `python -m py_compile`.
- Backend smoke test passed in a temporary virtualenv with minimal app dependencies installed:
  - `uvicorn main:app --app-dir apps/slide-api --host 127.0.0.1 --port 8765`
  - `curl http://127.0.0.1:8765/api/health` returned `ok=true`.
- Existing slide pipeline entrypoints were left in place and are invoked unchanged by the backend wrapper.

Notes:
- This packaging layer does not change slide compiler semantics.
- Frontend is Vercel-friendly; backend is intended for a persistent Python host.
- Runtime config is now repository-safe; real API keys are expected from environment variables during deployment.

## Slide Deployment Docs (2026-03-09)

Problem:
- The app packaging existed, but the repo still lacked one top-level deployment guide telling the owner how to run the backend on a remote server and connect the frontend deployment.

Solution:
- Added `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/README.md` as the main deployment guide.
- Added `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/deploy/systemd/slide-api.service` as a systemd example.
- Added `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/deploy/nginx/slide-api.conf` as an nginx reverse proxy example.

Validation:
- Documentation now covers:
  - GitHub push precautions
  - backend virtualenv setup
  - manual API smoke test
  - systemd install flow
  - nginx reverse proxy install flow
  - Vercel frontend deploy flow

Notes:
- This is a deployment/documentation layer only; no slide compiler behavior changed.

## Slide Placeholder Refill Wiring + Semantic Vertical Anchoring (2026-03-09)

Problem:
- The slide batch wrapper `gemini_svg_pipeline.py` did not pass `--refill-placeholders` through to the SVG->PPTX compiler, so web-app runs could finish without placeholder image refill even when source PNG pages were available.
- Separately, some slide semantic textboxes appeared visually shifted upward in compiled PPTX when the SVG textbox bbox was taller than the actual text content. This is consistent with PPT text frames defaulting to top anchoring unless explicitly configured.

Fix:
- Added `--refill-placeholders` and `--refill-mode` to `gemini_svg_pipeline.py` and forwarded them into the compiler invocation.
- When refill is enabled, the wrapper now also passes the matching per-group `--source-image-dir`, allowing source-crop refill from the original page PNG directory.
- Updated the app backend `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-api/main.py` to enable placeholder refill by default with `--refill-mode source-crop`.
- Updated semantic textbox reconstruction in `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/gemini_pipeline/compiler/pptx/semantic_text.py`:
  - zero paragraph `space_before` / `space_after`
  - heuristic `vertical_anchor = middle` when textbox bbox is materially taller than the estimated text content height
  - explicit `vertical_anchor = top` for absolute text items

Validation:
- `python -m py_compile` passed for:
  - `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/gemini_pipeline/gemini_svg_pipeline.py`
  - `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/gemini_pipeline/compiler/pptx/semantic_text.py`
  - `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-api/main.py`
- `python gemini_pipeline/gemini_svg_pipeline.py --help` now shows:
  - `--refill-placeholders`
  - `--refill-mode {gemini,source-crop,auto}`
- Backend smoke test still passed after the app command change:
  - `uvicorn main:app --app-dir apps/slide-api --host 127.0.0.1 --port 8766`
  - `curl http://127.0.0.1:8766/api/health` returned `ok=true`.

Notes:
- The vertical anchor change is a generic slide semantic-text fix, not a per-file adjustment.
- The wrapper still leaves refill opt-in at the CLI level, but the packaged app backend now enables it by default for uploaded PDFs.

## Slide App Artifact Persistence + Publish Hygiene (2026-03-10)

Problem:
- The app backend already kept job workspaces on disk, but this was not explicit enough in the API or deployment docs.
- The local `slide-stable` worktree also accumulated generated `input/`, `output/`, and runtime job data, which should not be pushed to GitHub.

Fix:
- Added `.gitignore` at `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/.gitignore` to ignore:
  - generated `gemini_pipeline/input/`
  - generated `gemini_pipeline/output/`
  - runtime `gemini_pipeline/app_data/jobs/`
  - local `.DS_Store` and venv noise
- Updated `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-api/main.py`:
  - job status now exposes:
    - source PDF URL
    - per-page PNG URLs
    - per-page SVG URLs
    - final PPTX URL
    - persisted artifact layout metadata
  - added source PDF endpoint
  - added rendered PNG endpoint
  - appends artifact storage paths into job logs
- Updated deployment docs to explicitly describe the persistent artifact layout under `app_data/jobs/<jobId>/`.

Validation:
- The backend still preserves one persistent job directory per upload.
- Input and output artifacts are now explicit in API responses and documentation.
- Generated local test inputs/outputs are ignored by Git and no longer pollute release pushes.

## Slide App Frontend-Exposed Refill Strategy (2026-03-10)

Problem:
- The packaged slide app hard-coded placeholder refill to `source-crop`.
- Lab users could not choose between source cropping, image regeneration, automatic fallback, or disabling refill.

Fix:
- Updated `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-web/index.html` and `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-web/app.js` to expose a frontend `Image Placeholder Strategy` selector.
- Updated `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-api/main.py` to accept multipart form field `refill_mode`, validate it, store it in job metadata, and pass it through to `gemini_svg_pipeline.py`.
- Supported app-level values:
  - `source-crop`
  - `gemini`
  - `auto`
  - `off`

Validation:
- Backend route signature now accepts `refill_mode: Form(...)`.
- Job serialization now exposes `settings.refillMode`, making the chosen mode visible in the frontend and logs.

## Release Hygiene for Archived Utilities (2026-03-10)

Problem:
- Several scripts under `gemini_pipeline/past/20260305_pipeline_cleanup/` still contained hard-coded historical API keys.
- Even though these scripts are archived, leaving them unchanged would make the repository unsafe to publish to GitHub.

Fix:
- Replaced embedded keys in archived helper scripts with environment variable reads:
  - `OPENAI_API_KEY`
  - `GEMINI_API_KEY`

Validation:
- A full repository scan over the publishable `slide-stable` tree no longer finds live-looking API keys.


## 2026-03-10 App Runtime Controls

- Extended the slide app so users can provide per-job request settings from the frontend:
  - request provider: `openai-compatible` or `gemini-native`
  - model API base
  - code model
  - image model
  - image placeholder refill mode
- Backend changes in `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-api/main.py`:
  - accept multipart form fields `request_provider`, `request_api_base`, `request_api_key`, `default_model`, `image_model`, `refill_mode`
  - write a job-scoped `runtime_config.json` under `gemini_pipeline/app_data/jobs/<jobId>/`
  - pass that config into `gemini_svg_pipeline.py --config ...`
  - return non-secret settings in job status
  - redact `OPENAI_API_KEY`, `GEMINI_API_KEY`, `api_key`, and `image_api_key` from the saved runtime config after execution
- Frontend changes in `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-web/`:
  - added controls for provider, model API base, API key, code model, image model, and refill strategy
  - persist non-secret fields in browser local storage
  - do not persist the API key in browser local storage
- Compiler config changes:
  - `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/gemini_pipeline/compiler/io/config_loader.py` now maps `api_key` from `GEMINI_API_KEY` when needed
  - `/Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/gemini_pipeline/compiler/pptx/chart_codegen.py` now supports both OpenAI-compatible `chat/completions` and Gemini native `generateContent`
