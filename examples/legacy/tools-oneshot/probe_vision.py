"""验证 MaaS 通道的视觉能力：画一张带文字和颜色的图，问模型看到了什么。

只打印回答，不打印 key。
"""
import base64
import io
import json
import urllib.request

from PIL import Image, ImageDraw

BASE = "https://maas.qianwenaiapi.com/compatible-mode/v1"
KEY = json.load(open("E:/dybot/data/config.json", encoding="utf-8"))["deepseek"]["apiKey"]


def make_img() -> str:
    img = Image.new("RGB", (320, 160), (18, 92, 200))
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 90, 90], fill=(240, 60, 40))
    d.text((120, 60), "HELLO 7", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def ask(model: str, want_image: bool) -> str:
    content = [{"type": "text", "text": "这张图里有什么？说清背景颜色、左边那个方块的形状和颜色、图上的字。"}]
    if want_image:
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + make_img()}})
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 300,
        "reasoning_effort": "none",
    }
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())
    return (data["choices"][0]["message"].get("content") or "").strip()


if __name__ == "__main__":
    for model in ("deepseek-v4.1-flash", "qwen3.8-flash", "qwen3.7-flash"):
        for want in (True, False):
            tag = "带图" if want else "纯文本"
            try:
                out = ask(model, want)
                print(f"[{model} {tag}] {out[:160]}")
            except Exception as exc:
                body = ""
                if hasattr(exc, "read"):
                    try:
                        body = exc.read().decode()[:160]
                    except Exception:
                        pass
                print(f"[{model} {tag}] FAIL {str(exc)[:80]} {body}")
