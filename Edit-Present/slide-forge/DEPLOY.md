# Slide Forge 部署说明（本机 + Vercel）

## 快速检查

- 后端已在本机 8001 运行：`curl http://127.0.0.1:8001/api/health` 应返回 `{"ok":true,...}`。
- **HTTPS 隧道**：Vercel 是 HTTPS，后端需通过隧道暴露。当前使用 localtunnel，**隧道进程需常驻**（见下方）。
- 前端：https://slide-web-six.vercel.app（`config.js` 已指向隧道地址）

## 架构

- **后端**：本机 8001（slide-api + gemini_pipeline）
- **隧道**：`npx localtunnel --port 8001` 得到 `https://xxx.loca.lt`，供前端跨域 HTTPS 访问
- **前端**：Vercel 静态站点 https://slide-web-six.vercel.app

## 一、后端（本机 8001 端口）

### 1. 安装依赖（首次）

```bash
cd /mnt/cache/liwenbo/PPT2SVG-SlideSVG/Edit-Present/slide-forge
python3 -m venv .venv
source .venv/bin/activate
pip install -r apps/slide-api/requirements.txt
```

### 2. 配置 API 密钥

编辑 `gemini_pipeline/config/runtime_api_config.json`，填入实际使用的 API 配置（勿提交到公开仓库）。

### 3. 启动方式

**方式 A：前台运行（调试）**

```bash
cd /mnt/cache/liwenbo/PPT2SVG-SlideSVG/Edit-Present/slide-forge
source .venv/bin/activate
./scripts/start-api.sh
```

**方式 B：后台运行**

```bash
cd /mnt/cache/liwenbo/PPT2SVG-SlideSVG/Edit-Present/slide-forge
source .venv/bin/activate
nohup uvicorn main:app --app-dir apps/slide-api --host 0.0.0.0 --port 8001 > /tmp/slide-api.log 2>&1 &
```

**方式 C：systemd 常驻**

```bash
sudo cp deploy/systemd/slide-api-8001.service.example /etc/systemd/system/slide-api.service
# 若路径不同，先编辑 /etc/systemd/system/slide-api.service 中的 WorkingDirectory 和 ExecStart
sudo systemctl daemon-reload
sudo systemctl enable slide-api
sudo systemctl start slide-api
sudo systemctl status slide-api
```

### 4. 验证

```bash
curl http://127.0.0.1:8001/api/health
# 应返回 {"ok":true,...}
```

### 5. 防火墙

若外网无法访问，请放行 8001（本机直连时）。用隧道时只需本机 127.0.0.1:8001 可达即可。

---

## 二、HTTPS 隧道（Vercel 前端必须访问 HTTPS 后端）

在**运行后端的本机**上常驻执行（关闭终端会断，可用 screen/tmvm）：

```bash
cd /mnt/cache/liwenbo/PPT2SVG-SlideSVG/Edit-Present/slide-forge
./scripts/start-tunnel.sh
```

会输出类似 `your url is: https://xxxx.loca.lt`。**首次**使用新地址时需：
1. 把该地址填到 `apps/slide-web/config.js` 的 `apiBase`
2. 重新部署：`cd apps/slide-web && vercel --prod --yes`

当前已配置的隧道地址：`https://loud-foxes-admire.loca.lt`（若隧道重启会变，需按上两步更新并重部署）。

---

## 三、前端（Vercel）

前端已配置 `apps/slide-web/config.js` 的 `apiBase: "http://103.237.28.69:8001"`。

### 1. 安装 Vercel CLI（首次）

```bash
npm i -g vercel
```

### 2. 登录 Vercel（仅首次）

在终端执行一次，按提示用浏览器完成登录：

```bash
vercel login
```

### 3. 部署

```bash
cd /mnt/cache/liwenbo/PPT2SVG-SlideSVG/Edit-Present/slide-forge/apps/slide-web
vercel --yes
```

或正式环境：`vercel --prod --yes`。部署完成后会得到 `https://xxx.vercel.app`，发给同学即可使用。

### 5. 若通过 GitHub 部署

1. 将 slide-forge 推到 GitHub（可只包含 `apps/slide-web` 或整仓）。
2. 在 [vercel.com](https://vercel.com) 导入项目，**Root Directory 设为 `apps/slide-web`**。
3. 无需 build 命令，直接 Deploy（静态站点）。

---

## 四、同学使用流程

1. 打开你提供的 Vercel 链接。
2. 上传 PDF，选择图片占位策略。
3. 等待后端处理（轮询状态）。
4. 预览 SVG，下载 PPTX。

---

## 五、本机 IP 说明

- 公网 IP：`103.237.28.69`（外网访问用）
- 内网 IP：`10.119.18.6`（同网段访问用）

若公网 IP 变更，需修改 `apps/slide-web/config.js` 中的 `apiBase` 并重新部署前端。
