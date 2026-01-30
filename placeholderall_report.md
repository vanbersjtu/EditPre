# placeholderall 逻辑报告

## 文件位置
- `PPT2SVG-SlideSVG/data_process_src/placeholderall.py`
- 依赖：`PPT2SVG-SlideSVG/data_process_src/pipeline/svg_image_placeholder.py`

## 功能概览
`placeholderall.py` 用于批量遍历输入根目录下包含 SVG 的子目录，完成三段式处理：
1. **Placeholder**：生成占位符 SVG（不触发 VLM，仅保留尺寸/位置）。
2. **Caption & 图表判断**：对全局去重后的图片做英文 caption，并对“可疑图表”二次 VLM 判别。
3. **Replace**：将 caption/is_chart 回填到 SVG，占位大小保持一致。

全局缓存写入：
- `_global_cache/caption_cache.json`
- `_global_cache/chart_cache.json`
- `_global_cache/chart_captions.json`

## 输入/输出
- 输入：`--input` 根目录，子目录内含 `.SVG` 文件。
- 输出：`--output` 根目录，按相对目录结构写出处理后的 SVG。
- `_global_cache` 位于输入根目录下（`in_root/_global_cache`）。

## 核心流程（按执行顺序）
1. **扫描目录**
   - `find_svg_dirs` 只收集包含 `.SVG` 的目录，跳过 `_global_cache`。

2. **加载配置与全局缓存**
   - 从 `--config` 或环境变量读取 VLM 配置。
   - 非 `--force` 模式会加载全局 `caption_cache` / `chart_cache`，并补齐各子目录旧缓存。

3. **预处理：收集图片并去重**
   - `run_for_dir_collect` 调 `collect_unique_images` 提取图片、构建目录级 `image_cache.json`。
   - 汇总为 `all_unique_images`，跨目录按 hash 去重。

4. **阶段 1：Placeholder**
   - `run_replace_stage("Placeholder", ...)`：
     - `process_svg` 生成占位 SVG。
     - 不调用 VLM，仅使用已有缓存。

5. **阶段 2：Caption（全局去重批量）**
   - 仅对 `all_unique_images` 调 VLM，更新 `caption_cache_global`。
   - `--vlm-backend=api` 使用 `generate_captions`；
     `--vlm-backend=vllm-batch` 使用 `run_vllm_batch`（Ray Data + vLLM）。

6. **阶段 3：图表判定（可疑筛选 + 二次 VLM）**
   - `is_suspicious_chart_caption` 用关键词筛“可疑图表”。
   - `detect_charts_with_vlm` 或 vLLM 批量二次判断，仅返回 `is_chart`。
   - 未判定的 hash 默认置为 `False`。

7. **阶段 4：Replace**
   - `run_replace_stage("Replace", ...)`：
     - 将 caption/is_chart 回填 SVG。
     - 写出目录级：`image_placeholders.json`、`image_cache.json`、`caption_cache.json`、`chart_cache.json`。

8. **阶段 5：全局缓存更新**
   - 收集 `is_chart=True` 的条目，写入 `chart_captions.json`。
   - 保存全局 `caption_cache.json` 与 `chart_cache.json`。

## 关键数据结构/文件
- **目录级**
  - `image_cache.json`：source_key -> {hash, path, mime, has_alpha}
  - `image_placeholders.json`：SVG 内图片占位映射。
  - `caption_cache.json`：仅包含该目录涉及的 hash -> caption。
  - `chart_cache.json`：仅包含该目录涉及的 hash -> is_chart。

- **全局级（输入根目录下）**
  - `_global_cache/caption_cache.json`：hash -> caption。
  - `_global_cache/chart_cache.json`：hash -> is_chart。
  - `_global_cache/chart_captions.json`：hash -> {caption, is_chart}。

## VLM 细节与配置来源
- 优先顺序：命令行参数 > `config.json` > 环境变量。
- 关键参数：`--base-url`、`--api-key`、`--model`、`--max-tokens`、`--temperature`。
- `--image-token` 用于在 prompt 前追加图像 token（vLLM batch）。

## 并行与限流
- `--folder-workers`：按目录并行执行收集/replace。
- `--workers`：目录内 VLM 并行（API 后端）。
- `--qps` + `RateLimiter`：全局 QPS 控制。

## 跳过与容错行为
- 没有 VLM 配置时跳过图表二次判定。
- 图片路径缺失时 caption 默认 `图片占位`，chart 默认 `False`。
- `.SVG` 以 `._` 开头的文件会被跳过。

