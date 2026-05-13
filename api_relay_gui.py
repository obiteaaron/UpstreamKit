import json
import os
import argparse
import atexit
import tempfile
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import END, DISABLED, NORMAL, StringVar, Text, Tk, WORD, ttk

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None
    Image = None
    ImageDraw = None


APP_NAME = "UpstreamKit"
DEFAULT_PORT = "8787"
LOG_PREVIEW_LIMIT = 1200
SAFE_USER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
UNSAFE_USER_ID_CHAR_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def app_dir():
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin" and ".app/Contents/MacOS" in sys.executable:
            return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(sys.executable))))
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_DIR = app_dir()
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
TOKEN_STATS_PATH = os.path.join(CONFIG_DIR, "token_stats.json")
DEFAULT_CONFIG = {
    "provider": "openai",
    "base_url": "https://api.openai.com",
    "api_key": "",
    "model": "gpt-4.1",
    "port": DEFAULT_PORT,
}
DEFAULT_TOKEN_STATS = {
    "input_tokens": 0,
    "cache_miss_input_tokens": 0,
    "cache_hit_input_tokens": 0,
    "output_tokens": 0,
    "cache_known": False,
}


def now_text():
    return time.strftime("%H:%M:%S")


def join_url(base_url, path):
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return path
    if base.endswith("/v1") and path.startswith("/v1/"):
        return base + path[3:]
    return base + path


def post_json(url, headers, body, timeout=60):
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return resp.status, data.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        data = err.read()
        return err.code, data.decode("utf-8", errors="replace")


def strip_reasoning_content(body):
    for message in body.get("messages", []) if isinstance(body, dict) else []:
        if isinstance(message, dict):
            message.pop("reasoning_content", None)
            content = message.get("content")
            if isinstance(content, list):
                message["content"] = [
                    item for item in content
                    if not (isinstance(item, dict) and item.get("type") in ("thinking", "redacted_thinking"))
                ]


def preview_text(text, limit=LOG_PREVIEW_LIMIT):
    text = (text or "").replace("\r", " ").replace("\n", " ")
    if len(text) > limit:
        return text[:limit] + "...[truncated]"
    return text


def request_summary(body):
    if not isinstance(body, dict):
        return "body=non-json"
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    return (
        f"body_keys={list(body.keys())}, "
        f"client_model={body.get('model')}, "
        f"stream={body.get('stream')}, "
        f"messages={len(messages) if isinstance(messages, list) else 'n/a'}, "
        f"tools={len(tools) if isinstance(tools, list) else 'n/a'}, "
        f"max_tokens={body.get('max_tokens')}"
    )


def sanitize_user_id_value(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if SAFE_USER_ID_RE.match(text):
        return text
    sanitized = UNSAFE_USER_ID_CHAR_RE.sub("_", text).strip("_")
    if not sanitized:
        return None
    return sanitized[:128]


def sanitize_user_id_fields(body):
    changes = []
    if not isinstance(body, dict):
        return changes

    metadata = body.get("metadata")
    if isinstance(metadata, dict) and "user_id" in metadata:
        original = metadata.get("user_id")
        sanitized = sanitize_user_id_value(original)
        if sanitized:
            metadata["user_id"] = sanitized
            if sanitized != original:
                changes.append(f"metadata.user_id: {original!r} -> {sanitized!r}")
        else:
            metadata.pop("user_id", None)
            changes.append(f"metadata.user_id: {original!r} -> removed")

    if "user_id" in body:
        original = body.get("user_id")
        sanitized = sanitize_user_id_value(original)
        if sanitized:
            body["user_id"] = sanitized
            if sanitized != original:
                changes.append(f"user_id: {original!r} -> {sanitized!r}")
        else:
            body.pop("user_id", None)
            changes.append(f"user_id: {original!r} -> removed")

    return changes


def rough_token_count(body):
    def walk(value):
        if isinstance(value, str):
            return max(1, len(value) // 4)
        if isinstance(value, list):
            return sum(walk(item) for item in value)
        if isinstance(value, dict):
            return sum(walk(item) for item in value.values())
        return 0

    return max(1, walk(body.get("system", "")) + walk(body.get("messages", [])) + walk(body.get("tools", [])))


def create_tray_image():
    image = Image.new("RGB", (64, 64), "#2563eb")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 56, 56), radius=12, fill="#111827")
    draw.rectangle((18, 20, 46, 26), fill="#ffffff")
    draw.rectangle((18, 32, 46, 38), fill="#ffffff")
    draw.rectangle((18, 44, 36, 50), fill="#ffffff")
    return image


def load_saved_config():
    try:
        if not os.path.exists(CONFIG_PATH):
            save_config_data(DEFAULT_CONFIG)
            return dict(DEFAULT_CONFIG)
        with open(CONFIG_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, dict):
            merged = dict(DEFAULT_CONFIG)
            merged.update(data)
            return merged
    except Exception:
        return dict(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config_data(data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def normalize_token_stats(data):
    merged = dict(DEFAULT_TOKEN_STATS)
    if isinstance(data, dict):
        for key in DEFAULT_TOKEN_STATS:
            if key in data:
                merged[key] = data[key]
    for key in ("input_tokens", "cache_miss_input_tokens", "cache_hit_input_tokens", "output_tokens"):
        try:
            merged[key] = int(merged.get(key) or 0)
        except (TypeError, ValueError):
            merged[key] = 0
    merged["cache_known"] = bool(merged.get("cache_known"))
    return merged


def load_token_stats():
    try:
        if not os.path.exists(TOKEN_STATS_PATH):
            save_token_stats(DEFAULT_TOKEN_STATS)
            return dict(DEFAULT_TOKEN_STATS)
        with open(TOKEN_STATS_PATH, "r", encoding="utf-8") as file:
            return normalize_token_stats(json.load(file))
    except Exception:
        return dict(DEFAULT_TOKEN_STATS)


def save_token_stats(data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(TOKEN_STATS_PATH, "w", encoding="utf-8") as file:
        json.dump(normalize_token_stats(data), file, ensure_ascii=False, indent=2)


def extract_usage_tokens(payload, provider=None):
    usage = None
    if isinstance(payload, dict):
        usage = payload.get("usage")
        if usage is None and isinstance(payload.get("message"), dict):
            usage = payload["message"].get("usage")
    if not isinstance(usage, dict):
        return None

    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    cache_hit = int(
        usage.get("cache_read_input_tokens")
        or usage.get("cached_tokens")
        or 0
    )

    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        cache_hit = int(prompt_details.get("cached_tokens") or cache_hit)

    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    if "input_tokens" in usage:
        cache_miss = int(usage.get("input_tokens") or 0) + cache_creation
        input_tokens = cache_miss + cache_hit
        cache_known = any(key in usage for key in ("cache_creation_input_tokens", "cache_read_input_tokens"))
    elif "prompt_tokens" in usage:
        input_tokens = int(usage.get("prompt_tokens") or 0)
        cache_miss = max(0, input_tokens - cache_hit)
        cache_known = isinstance(prompt_details, dict) and "cached_tokens" in prompt_details
    else:
        input_tokens = 0
        cache_miss = cache_creation
        input_tokens = cache_miss + cache_hit
        cache_known = cache_hit > 0 or cache_creation > 0

    return {
        "input_tokens": input_tokens,
        "cache_miss_input_tokens": cache_miss,
        "cache_hit_input_tokens": cache_hit,
        "output_tokens": output_tokens,
        "cache_known": cache_known,
    }


def add_token_stats(base, delta):
    out = normalize_token_stats(base)
    if not delta:
        return out
    normalized = normalize_token_stats(delta)
    for key in ("input_tokens", "cache_miss_input_tokens", "cache_hit_input_tokens", "output_tokens"):
        out[key] += normalized[key]
    out["cache_known"] = out["cache_known"] or normalized["cache_known"]
    return out


@dataclass
class RelayConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    port: int


class RelayState:
    def __init__(self, config, log_func, token_func=None):
        self.config = config
        self.log = log_func
        self.record_tokens = token_func or (lambda usage: None)


def normalize_text_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, dict) and item.get("type") == "thinking":
                parts.append(item.get("thinking", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(x for x in parts if x)
    return str(content)


def anthropic_to_openai_request(body, model):
    messages = []
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": normalize_text_content(system)})

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            messages.append({"role": role, "content": normalize_text_content(content)})
            continue

        text_parts = []
        image_parts = []
        tool_calls = []
        tool_messages = []
        reasoning_content = []

        for item in content:
            if not isinstance(item, dict):
                text_parts.append(str(item))
                continue

            item_type = item.get("type")
            if item_type == "text":
                text_parts.append(item.get("text", ""))
            elif item_type == "thinking":
                reasoning_content.append(item.get("thinking", ""))
            elif item_type == "image":
                source = item.get("source", {})
                if source.get("type") == "base64":
                    media_type = source.get("media_type", "image/png")
                    image_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{source.get('data', '')}"},
                    })
            elif item_type == "tool_use":
                tool_calls.append({
                    "id": item.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": json.dumps(item.get("input", {}), ensure_ascii=False),
                    },
                })
            elif item_type == "tool_result":
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("tool_use_id", ""),
                    "content": normalize_text_content(item.get("content", "")),
                })

        if tool_messages:
            messages.extend(tool_messages)

        if role == "assistant" and tool_calls:
            assistant = {"role": "assistant", "content": "\n".join(x for x in text_parts if x) or None, "tool_calls": tool_calls}
            if reasoning_content:
                assistant["reasoning_content"] = "\n".join(reasoning_content)
            messages.append(assistant)
        elif image_parts:
            mixed = []
            if text_parts:
                mixed.append({"type": "text", "text": "\n".join(x for x in text_parts if x)})
            mixed.extend(image_parts)
            messages.append({"role": role, "content": mixed})
        elif text_parts or reasoning_content:
            out = {"role": role, "content": "\n".join(x for x in text_parts if x)}
            if role == "assistant" and reasoning_content:
                out["reasoning_content"] = "\n".join(reasoning_content)
            messages.append(out)

    openai_body = {
        "model": model,
        "messages": messages,
        "stream": bool(body.get("stream", False)),
    }

    for src, dst in [
        ("max_tokens", "max_tokens"),
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop_sequences", "stop"),
    ]:
        if src in body:
            openai_body[dst] = body[src]

    if "reasoning_effort" in body:
        openai_body["reasoning_effort"] = body["reasoning_effort"]

    if body.get("tools"):
        openai_body["tools"] = []
        for tool in body["tools"]:
            openai_body["tools"].append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            })

    return openai_body


def openai_to_anthropic_response(openai_body, model):
    choice = (openai_body.get("choices") or [{}])[0]
    message = choice.get("message", {})
    content = []

    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})

    for call in message.get("tool_calls") or []:
        fn = call.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.get("arguments", "")}
        content.append({
            "type": "tool_use",
            "id": call.get("id", ""),
            "name": fn.get("name", ""),
            "input": args,
        })

    finish_reason = choice.get("finish_reason")
    stop_reason = "tool_use" if finish_reason == "tool_calls" else "end_turn"
    if finish_reason == "length":
        stop_reason = "max_tokens"
    elif finish_reason == "stop":
        stop_reason = "end_turn"

    usage = openai_body.get("usage") or {}
    return {
        "id": openai_body.get("id", "msg_openai"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def sse_line(event=None, data=None):
    out = ""
    if event:
        out += f"event: {event}\n"
    if data is not None:
        out += "data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    return (out + "\n").encode("utf-8")


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "ApiRelay/1.0"

    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_json(200, {"ok": True, "name": APP_NAME})
            return
        self.send_error(404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw_body.decode("utf-8") or "{}")
            cfg = self.server.state.config
            req_id = f"{int(time.time() * 1000) % 1000000:06d}-{threading.get_ident() % 10000:04d}"
            self.request_id = req_id
            self.server.state.log(
                f"[{req_id}] 收到请求 {self.path}，已忽略客户端 key/model，思考参数跟随请求方，"
                f"使用 {cfg.provider} / {cfg.model}；{request_summary(body)}"
            )

            if "/count_tokens" in self.path:
                count = rough_token_count(body)
                self.server.state.log(f"[{req_id}] 本地处理 count_tokens，估算 input_tokens={count}")
                self.send_json(200, {"input_tokens": count})
                return

            if cfg.provider == "openai":
                self.handle_openai_provider(body, cfg)
            else:
                self.handle_anthropic_provider(raw_body, body, cfg)
        except Exception as exc:
            req_id = getattr(self, "request_id", "no-id")
            self.server.state.log(f"[{req_id}] 请求处理失败: {type(exc).__name__}: {exc}")
            self.server.state.log(f"[{req_id}] {traceback.format_exc().splitlines()[-1]}")
            if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
                self.server.state.log(f"[{req_id}] 客户端已经断开连接，无法继续写回响应")
                return
            try:
                self.send_json(500, {"error": {"message": str(exc), "type": "relay_error"}})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.server.state.log(f"[{req_id}] 写回 500 时客户端已经断开")

    def log_message(self, fmt, *args):
        return

    def send_json(self, status, data):
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def handle_anthropic_provider(self, raw_body, body, cfg):
        body = dict(body)
        body["model"] = cfg.model
        for change in sanitize_user_id_fields(body):
            self.server.state.log(f"[{getattr(self, 'request_id', 'no-id')}] 已修正 user_id：{change}")
        target = join_url(cfg.base_url, self.path if self.path.startswith("/v1/") else "/v1/messages")
        self.server.state.log(
            f"[{getattr(self, 'request_id', 'no-id')}] 转发到 Anthropic 上游 {target}；"
            f"思考参数跟随请求方"
        )
        response = self.forward_json(target, cfg.api_key, body, provider="anthropic", stream=bool(body.get("stream")))
        self.copy_response(response)

    def handle_openai_provider(self, body, cfg):
        if self.path.endswith("/chat/completions"):
            outgoing = dict(body)
            outgoing["model"] = cfg.model
            for change in sanitize_user_id_fields(outgoing):
                self.server.state.log(f"[{getattr(self, 'request_id', 'no-id')}] 已修正 user_id：{change}")
        else:
            outgoing = anthropic_to_openai_request(body, cfg.model)

        target = join_url(cfg.base_url, "/v1/chat/completions")
        self.server.state.log(
            f"[{getattr(self, 'request_id', 'no-id')}] 转发到 OpenAI 上游 {target}；"
            f"思考参数跟随请求方"
        )
        if outgoing.get("stream"):
            response = self.forward_json(target, cfg.api_key, outgoing, provider="openai", stream=True)
            if self.path.endswith("/chat/completions"):
                self.copy_response(response)
            else:
                self.stream_openai_as_anthropic(response, cfg.model)
        else:
            response = self.forward_json(target, cfg.api_key, outgoing, provider="openai", stream=False)
            data = response.read()
            if response.status >= 400:
                self.server.state.log(
                    f"[{getattr(self, 'request_id', 'no-id')}] 上游错误响应体预览："
                    f"{preview_text(data.decode('utf-8', errors='replace'))}"
                )
                self.send_raw(response.status, response.headers.get("Content-Type", "application/json"), data)
                return
            openai_body = json.loads(data.decode("utf-8") or "{}")
            self.record_usage_from_payload(openai_body)
            if self.path.endswith("/chat/completions"):
                self.send_json(response.status, openai_body)
            else:
                self.send_json(200, openai_to_anthropic_response(openai_body, cfg.model))

    def forward_json(self, url, api_key, body, provider, stream):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": APP_NAME,
        }
        if provider == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = self.headers.get("anthropic-version", "2023-06-01")
            if self.headers.get("anthropic-beta"):
                headers["anthropic-beta"] = self.headers.get("anthropic-beta")
        else:
            headers["Authorization"] = f"Bearer {api_key}"

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        req_id = getattr(self, "request_id", "no-id")
        self.server.state.log(
            f"[{req_id}] 上游请求准备完成：provider={provider}, stream={stream}, "
            f"bytes={len(payload)}, {request_summary(body)}"
        )
        try:
            response = urllib.request.urlopen(req, timeout=600)
            self.server.state.log(
                f"[{req_id}] 上游已响应：HTTP {getattr(response, 'status', 'unknown')} "
                f"content-type={response.headers.get('Content-Type', '')}"
            )
            return response
        except urllib.error.HTTPError as err:
            self.server.state.log(
                f"[{req_id}] 上游返回错误：HTTP {err.code} "
                f"content-type={err.headers.get('Content-Type', '')}"
            )
            return err
        except urllib.error.URLError as err:
            self.server.state.log(f"[{req_id}] 上游连接失败：{err}")
            raise

    def send_raw(self, status, content_type, data):
        self.send_response(status)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def copy_response(self, response):
        req_id = getattr(self, "request_id", "no-id")
        content_type = response.headers.get("Content-Type", "application/json")
        if "text/event-stream" in content_type:
            self.send_response(getattr(response, "status", getattr(response, "code", 200)))
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.server.state.log(f"[{req_id}] 开始向客户端转发流式响应")
            event_name = None
            data_lines = []
            event_counts = {}
            event_samples = 0
            stream_usage = dict(DEFAULT_TOKEN_STATS)

            def finish_event():
                nonlocal event_name, data_lines, event_samples, stream_usage
                if event_name is None and not data_lines:
                    return
                name = event_name or "message"
                event_counts[name] = event_counts.get(name, 0) + 1
                data_text = "\n".join(data_lines)
                usage_delta = self.usage_from_sse_data(data_text)
                if usage_delta:
                    stream_usage = add_token_stats(stream_usage, usage_delta)
                if event_samples < 12:
                    self.log_sse_event(req_id, name, data_text)
                    event_samples += 1
                elif name in ("error", "message_stop"):
                    self.log_sse_event(req_id, name, data_text)
                event_name = None
                data_lines = []

            for line in response:
                self.wfile.write(line)
                self.wfile.flush()
                stripped = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if stripped == "":
                    finish_event()
                elif stripped.startswith("event:"):
                    event_name = stripped[6:].strip()
                elif stripped.startswith("data:"):
                    data_lines.append(stripped[5:].strip())
            finish_event()
            if stream_usage["input_tokens"] or stream_usage["output_tokens"]:
                self.server.state.record_tokens(stream_usage)
                self.server.state.log(
                    f"[{req_id}] token统计：input={stream_usage['input_tokens']}, "
                    f"cache_hit={stream_usage['cache_hit_input_tokens']}, "
                    f"output={stream_usage['output_tokens']}"
                )
            self.server.state.log(f"[{req_id}] 流式响应转发结束，events={event_counts}")
            self.close_connection = True
            return

        data = response.read()
        status = getattr(response, "status", getattr(response, "code", 200))
        if status >= 400:
            self.server.state.log(f"[{req_id}] 上游错误响应体预览：{preview_text(data.decode('utf-8', errors='replace'))}")
        else:
            self.server.state.log(f"[{req_id}] 上游成功响应：HTTP {status}, bytes={len(data)}")
            try:
                self.record_usage_from_payload(json.loads(data.decode("utf-8") or "{}"))
            except json.JSONDecodeError:
                pass
        self.send_raw(status, content_type, data)

    def stream_openai_as_anthropic(self, response, model):
        req_id = getattr(self, "request_id", "no-id")
        self.send_response(response.status)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.server.state.log(f"[{req_id}] 开始将 OpenAI 流式响应转换为 Anthropic SSE")

        msg_id = f"msg_{int(time.time() * 1000)}"
        self.wfile.write(sse_line("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }))

        text_started = False
        tool_chunks = {}
        text_index = 0
        stream_usage = dict(DEFAULT_TOKEN_STATS)

        for line in response:
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            try:
                chunk = json.loads(payload.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            usage_delta = extract_usage_tokens(chunk)
            if usage_delta:
                stream_usage = add_token_stats(stream_usage, usage_delta)

            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta", {})
            text = delta.get("content") or ""
            if text:
                if not text_started:
                    text_started = True
                    self.wfile.write(sse_line("content_block_start", {
                        "type": "content_block_start",
                        "index": text_index,
                        "content_block": {"type": "text", "text": ""},
                    }))
                self.wfile.write(sse_line("content_block_delta", {
                    "type": "content_block_delta",
                    "index": text_index,
                    "delta": {"type": "text_delta", "text": text},
                }))

            for call in delta.get("tool_calls") or []:
                idx = call.get("index", 0)
                current = tool_chunks.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if call.get("id"):
                    current["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    current["name"] = fn["name"]
                if fn.get("arguments"):
                    current["arguments"] += fn["arguments"]

        if text_started:
            self.wfile.write(sse_line("content_block_stop", {"type": "content_block_stop", "index": text_index}))

        next_index = 1 if text_started else 0
        for item in tool_chunks.values():
            try:
                args = json.loads(item["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {"_raw": item["arguments"]}
            self.wfile.write(sse_line("content_block_start", {
                "type": "content_block_start",
                "index": next_index,
                "content_block": {
                    "type": "tool_use",
                    "id": item["id"] or f"call_{next_index}",
                    "name": item["name"],
                    "input": args,
                },
            }))
            self.wfile.write(sse_line("content_block_stop", {"type": "content_block_stop", "index": next_index}))
            next_index += 1

        stop_reason = "tool_use" if tool_chunks else "end_turn"
        self.wfile.write(sse_line("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 0},
        }))
        self.wfile.write(sse_line("message_stop", {"type": "message_stop"}))
        if stream_usage["input_tokens"] or stream_usage["output_tokens"]:
            self.server.state.record_tokens(stream_usage)
            self.server.state.log(
                f"[{req_id}] token统计：input={stream_usage['input_tokens']}, "
                f"cache_hit={stream_usage['cache_hit_input_tokens']}, "
                f"output={stream_usage['output_tokens']}"
            )
        self.server.state.log(f"[{req_id}] OpenAI 流式转换结束，tool_calls={len(tool_chunks)}, text_started={text_started}")
        self.close_connection = True

    def record_usage_from_payload(self, payload):
        usage_delta = extract_usage_tokens(payload)
        if not usage_delta:
            return
        self.server.state.record_tokens(usage_delta)
        self.server.state.log(
            f"[{getattr(self, 'request_id', 'no-id')}] token统计："
            f"input={usage_delta['input_tokens']}, "
            f"cache_hit={usage_delta['cache_hit_input_tokens']}, "
            f"output={usage_delta['output_tokens']}"
        )

    def usage_from_sse_data(self, data_text):
        if not data_text:
            return None
        try:
            payload = json.loads(data_text)
        except json.JSONDecodeError:
            return None
        usage_delta = extract_usage_tokens(payload)
        if usage_delta:
            return usage_delta
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            return extract_usage_tokens({"usage": payload["usage"]})
        return None

    def log_sse_event(self, req_id, event_name, data_text):
        if not data_text:
            self.server.state.log(f"[{req_id}] SSE event={event_name}")
            return
        try:
            data = json.loads(data_text)
        except json.JSONDecodeError:
            self.server.state.log(f"[{req_id}] SSE event={event_name}, data={preview_text(data_text, 300)}")
            return

        summary = f"[{req_id}] SSE event={event_name}"
        data_type = data.get("type") if isinstance(data, dict) else None
        if data_type:
            summary += f", type={data_type}"
        if isinstance(data, dict) and data_type == "content_block_start":
            block = data.get("content_block") or {}
            summary += f", block_type={block.get('type')}, name={block.get('name')}"
        elif isinstance(data, dict) and data_type == "content_block_delta":
            delta = data.get("delta") or {}
            text = delta.get("text") or delta.get("partial_json") or ""
            summary += f", delta_type={delta.get('type')}, text={preview_text(text, 160)}"
        elif isinstance(data, dict) and data_type == "message_delta":
            delta = data.get("delta") or {}
            summary += f", stop_reason={delta.get('stop_reason')}"
        elif isinstance(data, dict) and data_type == "error":
            summary += f", error={preview_text(json.dumps(data, ensure_ascii=False), 500)}"
        self.server.state.log(summary)


class RelayApp:
    def __init__(self, autostart=False):
        # 单实例保护
        self._lockfile = os.path.join(tempfile.gettempdir(), "UpstreamKit.lock")
        if os.path.exists(self._lockfile):
            try:
                with open(self._lockfile, "r") as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, 0)  # 检查进程是否存在
                self.log = lambda _: None  # 占位
                print(f"UpstreamKit 已在运行 (PID {old_pid})，退出。")
                sys.exit(0)
            except (OSError, ValueError):
                os.remove(self._lockfile)  # 僵尸锁，清理
        with open(self._lockfile, "w") as f:
            f.write(str(os.getpid()))
        atexit.register(lambda: os.path.exists(self._lockfile) and os.remove(self._lockfile))
        #单实例保护结束
        self.root = Tk()
        self.root.title(APP_NAME)
        self.root.geometry("860x620")
        self.root.minsize(760, 560)

        self.saved_config = load_saved_config()
        self.provider_var = StringVar(value=self.saved_config.get("provider", "openai"))
        self.url_var = StringVar(value=self.saved_config.get("base_url", "https://api.openai.com"))
        self.key_var = StringVar(value=self.saved_config.get("api_key", ""))
        self.model_var = StringVar(value=self.saved_config.get("model", "gpt-4.1"))
        self.port_var = StringVar(value=str(self.saved_config.get("port", DEFAULT_PORT)))
        self.output_var = StringVar(value="未运行")
        self.session_tokens = dict(DEFAULT_TOKEN_STATS)
        self.total_tokens = load_token_stats()
        self.token_lock = threading.Lock()
        self.session_token_var = StringVar()
        self.total_token_var = StringVar()

        self.server = None
        self.server_thread = None
        self.tray_icon = None
        self.tray_thread = None
        self.exiting = False
        self.autostart = autostart
        self.close_hint_logged = False
        self.log_queue = queue.Queue()
        self.log_lines = []

        self.build_ui()
        self.update_token_labels()
        self.setup_config_autosave()
        self.start_tray_icon()
        self.root.after(100, self.flush_logs)

    def build_ui(self):
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="上游 URL").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.url_var).grid(row=0, column=1, columnspan=3, sticky="ew", pady=6)

        ttk.Label(frame, text="上游 Key").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.key_var, show="*").grid(row=1, column=1, columnspan=3, sticky="ew", pady=6)

        ttk.Label(frame, text="上游类型").grid(row=2, column=0, sticky="w", pady=6)
        provider_box = ttk.Combobox(frame, textvariable=self.provider_var, values=["openai", "anthropic"], state="readonly", width=18)
        provider_box.grid(row=2, column=1, sticky="w", pady=6)
        provider_box.bind("<<ComboboxSelected>>", self.on_provider_change)

        ttk.Label(frame, text="上游模型（实际请求）").grid(row=2, column=2, sticky="e", pady=6)
        ttk.Entry(frame, textvariable=self.model_var, width=28).grid(row=2, column=3, sticky="ew", pady=6)

        ttk.Label(frame, text="本地端口").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Entry(frame, textvariable=self.port_var, width=18).grid(row=3, column=1, sticky="w", pady=6)

        self.run_button = ttk.Button(frame, text="运行", command=self.toggle_server)
        self.run_button.grid(row=3, column=3, sticky="e", pady=6)

        self.test_button = ttk.Button(frame, text="测试", command=self.test_upstream)
        self.test_button.grid(row=4, column=3, sticky="e", pady=6)

        sep = ttk.Separator(frame)
        sep.grid(row=5, column=0, columnspan=4, sticky="ew", pady=12)

        ttk.Label(frame, text="请求中转的 URL").grid(row=6, column=0, sticky="nw", pady=6)
        output = ttk.Entry(frame, textvariable=self.output_var, state="readonly")
        output.grid(row=6, column=1, columnspan=3, sticky="ew", pady=6)

        info = "Claude Code 开发者模式里填上方 URL；Key 可留空或随便填；客户端传来的 model 会被忽略，思考参数会保留。"
        ttk.Label(frame, text=info).grid(row=7, column=1, columnspan=3, sticky="w", pady=3)

        ttk.Label(frame, text="本次开启token：").grid(row=8, column=0, sticky="nw", pady=(10, 2))
        ttk.Label(frame, textvariable=self.session_token_var).grid(row=8, column=1, columnspan=3, sticky="w", pady=(10, 2))

        ttk.Label(frame, text="总计token：").grid(row=9, column=0, sticky="nw", pady=2)
        ttk.Label(frame, textvariable=self.total_token_var).grid(row=9, column=1, columnspan=3, sticky="w", pady=2)

        ttk.Label(frame, text="日志").grid(row=10, column=0, sticky="nw", pady=(14, 6))
        log_frame = ttk.Frame(frame)
        log_frame.grid(row=10, column=1, columnspan=3, sticky="nsew", pady=(14, 0))

        self.log_text = Text(log_frame, wrap=WORD, height=15, state=DISABLED)
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)
        frame.rowconfigure(10, weight=1)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def setup_config_autosave(self):
        for var in (self.provider_var, self.url_var, self.key_var, self.model_var, self.port_var):
            var.trace_add("write", self.schedule_config_save)

    def schedule_config_save(self, *_args):
        if hasattr(self, "save_after_id") and self.save_after_id:
            self.root.after_cancel(self.save_after_id)
        self.save_after_id = self.root.after(500, self.save_current_config)

    def save_current_config(self):
        self.save_after_id = None
        data = {
            "provider": self.provider_var.get(),
            "base_url": self.url_var.get(),
            "api_key": self.key_var.get(),
            "model": self.model_var.get(),
            "port": self.port_var.get(),
        }
        try:
            save_config_data(data)
        except Exception as exc:
            self.log(f"保存配置失败：{exc}")

    def format_token_line(self, stats):
        stats = normalize_token_stats(stats)
        if stats["cache_known"]:
            return (
                f"输入（未命中）{stats['cache_miss_input_tokens']}    "
                f"输入（命中）{stats['cache_hit_input_tokens']}    "
                f"输出{stats['output_tokens']}"
            )
        return f"输入 {stats['input_tokens']}    输出 {stats['output_tokens']}"

    def update_token_labels(self):
        self.session_token_var.set(self.format_token_line(self.session_tokens))
        self.total_token_var.set(self.format_token_line(self.total_tokens))

    def record_token_usage(self, usage_delta):
        if not usage_delta:
            return
        with self.token_lock:
            self.session_tokens = add_token_stats(self.session_tokens, usage_delta)
            self.total_tokens = add_token_stats(self.total_tokens, usage_delta)
            try:
                save_token_stats(self.total_tokens)
            except Exception as exc:
                self.log(f"保存 token 统计失败：{exc}")
        self.root.after(0, self.update_token_labels)

    def on_provider_change(self, _event=None):
        if self.provider_var.get() == "anthropic":
            if "openai.com" in self.url_var.get():
                self.url_var.set("https://api.anthropic.com")
            if self.model_var.get() == "gpt-4.1":
                self.model_var.set("claude-3-5-sonnet-latest")
        else:
            if "anthropic.com" in self.url_var.get():
                self.url_var.set("https://api.openai.com")

    def toggle_server(self):
        if self.server:
            self.stop_server()
        else:
            self.start_server()

    def read_config_from_form(self):
        port = int(self.port_var.get().strip())
        if not (1 <= port <= 65535):
            raise ValueError("端口必须在 1-65535 之间")
        if not self.url_var.get().strip():
            raise ValueError("请填写上游 URL")
        if not self.key_var.get().strip():
            raise ValueError("请填写上游 Key")
        if not self.model_var.get().strip():
            raise ValueError("请填写上游模型（实际请求）")
        return RelayConfig(
            provider=self.provider_var.get(),
            base_url=self.url_var.get().strip(),
            api_key=self.key_var.get().strip(),
            model=self.model_var.get().strip(),
            port=port,
        )

    def test_upstream(self):
        try:
            config = self.read_config_from_form()
        except Exception as exc:
            self.log(f"测试失败：{exc}")
            return

        self.test_button.configure(state=DISABLED)
        self.log(
            "开始测试上游："
            f"{config.provider} {config.base_url}，模型 {config.model}"
        )
        threading.Thread(target=self.run_upstream_test, args=(config,), daemon=True).start()

    def run_upstream_test(self, config):
        try:
            if config.provider == "openai":
                url = join_url(config.base_url, "/v1/chat/completions")
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {config.api_key}",
                    "User-Agent": APP_NAME,
                }
                body = {
                    "model": config.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 8,
                    "stream": False,
                }
            else:
                url = join_url(config.base_url, "/v1/messages")
                headers = {
                    "Content-Type": "application/json",
                    "x-api-key": config.api_key,
                    "anthropic-version": "2023-06-01",
                    "User-Agent": APP_NAME,
                }
                body = {
                    "model": config.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 8,
                    "stream": False,
                }
            status, text = post_json(url, headers, body, timeout=60)
            preview = text.replace("\r", " ").replace("\n", " ")[:500]
            if 200 <= status < 300:
                self.log(f"测试成功：HTTP {status}")
            else:
                self.log(f"测试失败：HTTP {status} {preview}")
        except Exception as exc:
            self.log(f"测试失败：{exc}")
        finally:
            self.root.after(0, lambda: self.test_button.configure(state=NORMAL))

    def start_server(self):
        if self.server:
            return
        try:
            self.save_current_config()
            config = self.read_config_from_form()
            state = RelayState(config, self.log, self.record_token_usage)
            server = ThreadingHTTPServer(("127.0.0.1", config.port), RelayHandler)
            server.state = state
            self.server = server
            self.server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            self.server_thread.start()
            local_url = f"http://127.0.0.1:{config.port}"
            self.output_var.set(local_url)
            self.run_button.configure(text="停止")
            self.log(f"已启动：{local_url}")
            self.log(
                f"上游：{config.provider} {config.base_url}，上游模型（实际请求）为 {config.model}，思考参数跟随请求方"
            )
        except Exception as exc:
            self.log(f"启动失败：{exc}")

    def stop_server(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            self.server_thread = None
        self.output_var.set("未运行")
        self.run_button.configure(text="运行")
        self.log("已停止")

    def on_close(self):
        self.hide_to_tray()

    def start_tray_icon(self):
        if pystray is None:
            self.log("托盘依赖不可用：关闭窗口会隐藏，但没有右键托盘菜单。打包版会包含托盘依赖。")
            return

        def show_action(_icon=None, _item=None):
            self.root.after(0, self.show_window)

        def exit_action(_icon=None, _item=None):
            self.root.after(0, self.exit_app)

        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", show_action, default=True),
            pystray.MenuItem("退出", exit_action),
        )
        self.tray_icon = pystray.Icon("UpstreamKit", create_tray_image(), APP_NAME, menu)
        self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        self.tray_thread.start()

    def hide_to_tray(self):
        self.root.withdraw()
        if not self.close_hint_logged:
            self.log("窗口已隐藏到系统托盘；右键托盘图标选择“退出”才会真正关闭程序。")
            self.close_hint_logged = True

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def exit_app(self):
        if os.path.exists(self._lockfile):
            try:
                os.remove(self._lockfile)
            except:
                pass
        if self.exiting:
            return
        self.exiting = True
        self.log("正在退出程序")
        self.stop_server()
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self.root.destroy()

    def log(self, message):
        self.log_queue.put((now_text(), message))

    def flush_logs(self):
        while True:
            try:
                item = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_lines.append(item)
            self.log_text.configure(state=NORMAL)
            self.log_text.insert(END, f"{item[0]}  {item[1]}\n")
            if len(self.log_lines) > 1000:
                self.log_lines.pop(0)
                self.log_text.delete("1.0", "2.0")
            self.log_text.configure(state=DISABLED)
            self.log_text.see(END)
        self.root.after(100, self.flush_logs)

    def run(self):
        self.log("程序已就绪")
        self.log(f"配置文件：{CONFIG_PATH}")
        self.log(f"token统计文件：{TOKEN_STATS_PATH}")
        self.root.after(100, self.flush_logs)
        if self.autostart:
            self.root.after(500, self.start_server)
        self.root.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--autostart", action="store_true", help="启动后自动开始中转服务")
    args = parser.parse_args()
    RelayApp(autostart=args.autostart).run()
