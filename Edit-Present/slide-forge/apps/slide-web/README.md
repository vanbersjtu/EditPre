# slide-web

Static frontend for the slide pipeline app.

## What it does
- Upload one PDF
- Poll the backend job status
- Preview each generated SVG page
- Download the final PPTX

## Deploy to Vercel
Deploy this folder as a static site:

```bash
cd /Users/xiaoxiaobo/Downloads/sjtuwenber_slide_stable/Downloads/sjtuwenber/apps/slide-web
vercel
```

Before deploying, edit `config.js` and set:

```js
window.SLIDE_APP_CONFIG = {
  apiBase: "https://your-slide-api.example.com"
};
```

If you reverse-proxy the backend under the same origin, `apiBase` can stay empty.
