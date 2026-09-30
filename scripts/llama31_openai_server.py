#!/usr/bin/env python3
"""Small OpenAI-compatible chat server for local Hugging Face chat models.

The CUA-Sandbox agent only needs /v1/models and /v1/chat/completions.  Keeping this
server dependency-free avoids installing a second inference stack on the
evaluation host.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import torch
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer


class ContextLengthError(ValueError):
    pass


_HTML_MARKER = "\nHTML:\n"
_OBSERVATION_SUFFIX_MARKER = "\n\nCLICKABLE ELEMENTS"
_CHROME_TAGS = {"footer", "header", "nav", "noscript", "script", "style", "svg", "template"}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_ELEMENT_ATTRIBUTES = (
    "aria-label",
    "checked",
    "disabled",
    "href",
    "name",
    "placeholder",
    "role",
    "selected",
    "title",
    "type",
    "value",
)


def _clean_text(value: str, limit: int = 240) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if len(cleaned) > limit:
        return cleaned[: limit - 3].rstrip() + "..."
    return cleaned


class _ObservationHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.interactive: list[dict[str, Any]] = []
        self.visible_text: list[str] = []
        self._seen_visible: set[str] = set()
        self._tag_stack: list[tuple[str, bool, dict[str, Any] | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name: value or "" for name, value in attrs}
        tag = tag.lower()
        parent_suppressed = self._tag_stack[-1][1] if self._tag_stack else False
        suppressed = parent_suppressed or tag in _CHROME_TAGS or attr_map.get("aria-hidden") == "true"
        record = None
        semantic_id = attr_map.get("data-semantic-id")
        if semantic_id and not suppressed:
            record = {
                "tag": tag,
                "id": semantic_id,
                "attrs": {name: attr_map[name] for name in _ELEMENT_ATTRIBUTES if name in attr_map},
                "text": [],
            }
            self.interactive.append(record)
        if tag not in _VOID_TAGS:
            self._tag_stack.append((tag, suppressed, record))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS and self._tag_stack:
            self._tag_stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._tag_stack) - 1, -1, -1):
            if self._tag_stack[index][0] == tag:
                del self._tag_stack[index:]
                break

    def handle_data(self, data: str) -> None:
        text = _clean_text(data)
        if not text:
            return
        active_records = [record for _, _, record in self._tag_stack if record is not None]
        for record in active_records:
            record["text"].append(text)
        suppressed = self._tag_stack[-1][1] if self._tag_stack else False
        if not suppressed and not active_records and text not in self._seen_visible:
            self._seen_visible.add(text)
            self.visible_text.append(text)


def _append_with_budget(lines: list[str], values: list[str], char_limit: int) -> None:
    used = 0
    for value in values:
        remaining = char_limit - used
        if remaining <= 0:
            break
        rendered = value if len(value) <= remaining else value[: max(0, remaining - 3)].rstrip() + "..."
        lines.append(rendered)
        used += len(rendered) + 1


def compress_observation_message(
    content: str,
    *,
    interactive_char_limit: int,
    visible_char_limit: int,
) -> str:
    if _HTML_MARKER not in content or _OBSERVATION_SUFFIX_MARKER not in content:
        return content
    prefix, html_and_suffix = content.split(_HTML_MARKER, 1)
    html, suffix = html_and_suffix.split(_OBSERVATION_SUFFIX_MARKER, 1)
    parser = _ObservationHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # The element lists remain available even if a malformed page defeats parsing.
        parser = _ObservationHTMLParser()

    element_lines = []
    seen_ids = set()
    for element in parser.interactive:
        semantic_id = element["id"]
        if semantic_id in seen_ids:
            continue
        seen_ids.add(semantic_id)
        details = [f'{semantic_id} | {element["tag"]}']
        text = _clean_text(" ".join(element["text"]), 180)
        if text:
            details.append(f'text="{text}"')
        for name, value in element["attrs"].items():
            clean_value = _clean_text(value, 180)
            details.append(f'{name}="{clean_value}"' if clean_value else name)
        element_lines.append(" | ".join(details))

    output = [
        prefix,
        "",
        "ADAPTIVELY COMPRESSED OBSERVATION:",
        "Interactive elements:",
    ]
    _append_with_budget(output, element_lines, interactive_char_limit)
    output.extend(("", "Visible content:"))
    _append_with_budget(output, parser.visible_text, visible_char_limit)

    tabs_marker = "\n\nTABS ("
    if tabs_marker in suffix:
        tabs = "TABS (" + suffix.split(tabs_marker, 1)[1]
        output.extend(("", tabs[:2000]))
    return "\n".join(output)


def _layer_device_map(model_path: str, devices: list[str]) -> dict[str, str]:
    config = AutoConfig.from_pretrained(
        model_path,
        local_files_only=True,
    )
    layer_count = int(config.num_hidden_layers)
    layers_per_device = (layer_count + len(devices) - 1) // len(devices)
    device_map = {
        "model.embed_tokens": devices[0],
        "model.rotary_emb": devices[0],
        "model.norm": devices[-1],
        "lm_head": devices[-1],
    }
    for layer_index in range(layer_count):
        device_index = min(layer_index // layers_per_device, len(devices) - 1)
        device_map[f"model.layers.{layer_index}"] = devices[device_index]
    return device_map


class Worker:
    def __init__(self, model_path: str, devices: list[str]) -> None:
        if not devices:
            raise ValueError("a worker requires at least one CUDA device")
        self.devices = devices
        model_kwargs: dict[str, Any] = {
            "local_files_only": True,
            "torch_dtype": torch.bfloat16,
            "low_cpu_mem_usage": True,
        }
        if len(devices) > 1:
            model_kwargs["device_map"] = _layer_device_map(model_path, devices)
            self.model = AutoModelForCausalLM.from_pretrained(
                model_path,
                **model_kwargs,
            )
        else:
            self.model = AutoModelForCausalLM.from_pretrained(
                model_path,
                **model_kwargs,
            ).to(torch.device(devices[0]))
        self.input_device = self.model.get_input_embeddings().weight.device
        # Some model families keep rotary frequencies outside the state dict.
        rotary_emb = getattr(self.model.model, "rotary_emb", None)
        if rotary_emb is not None:
            rotary_emb.to(self.input_device)
        self.model.eval()
        self.lock = threading.Lock()


class State:
    def __init__(
        self,
        model_path: str,
        device_groups: list[list[str]],
        max_model_len: int,
        adaptive_observation_compression: bool,
    ) -> None:
        self.model_id = Path(model_path).name
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
        self.max_model_len = max_model_len
        self.adaptive_observation_compression = adaptive_observation_compression
        self.workers = [Worker(model_path, devices) for devices in device_groups]
        self.next_worker = 0
        self.dispatch_lock = threading.Lock()

    def _encode(self, messages: list[dict[str, Any]]):
        return self.tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_tensors="pt",
            return_dict=True,
            enable_thinking=False,
        )

    def _encode_with_adaptive_compression(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int,
    ):
        encoded = self._encode(messages)
        original_tokens = int(encoded["input_ids"].shape[1])
        if original_tokens + max_tokens <= self.max_model_len:
            return encoded, original_tokens, []
        if not self.adaptive_observation_compression:
            return encoded, original_tokens, []

        observation_indices = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and _HTML_MARKER in message["content"]
        ]
        if not observation_indices:
            return encoded, original_tokens, []

        levels: dict[int, int] = {}
        standard_order = observation_indices[:-1] + observation_indices[-1:]
        attempts = [(index, 1) for index in standard_order]
        attempts.extend((index, 2) for index in observation_indices)
        compact_messages = [dict(message) for message in messages]
        for index, level in attempts:
            if levels.get(index, 0) >= level:
                continue
            levels[index] = level
            if level == 1:
                interactive_limit, visible_limit = 10_000, 6_000
            else:
                interactive_limit, visible_limit = 3_000, 1_500
            compact_messages[index]["content"] = compress_observation_message(
                messages[index]["content"],
                interactive_char_limit=interactive_limit,
                visible_char_limit=visible_limit,
            )
            encoded = self._encode(compact_messages)
            prompt_tokens = int(encoded["input_ids"].shape[1])
            if prompt_tokens + max_tokens <= self.max_model_len:
                compressed = [f"{message_index}:L{message_level}" for message_index, message_level in sorted(levels.items())]
                print(
                    f"[context] adaptive observation compression "
                    f"tokens={original_tokens}->{prompt_tokens} messages={compressed}",
                    flush=True,
                )
                return encoded, prompt_tokens, compressed
        prompt_tokens = int(encoded["input_ids"].shape[1])
        compressed = [
            f"{message_index}:L{message_level}" for message_index, message_level in sorted(levels.items())
        ]
        print(
            f"[context] adaptive observation compression insufficient "
            f"tokens={original_tokens}->{prompt_tokens} messages={compressed}",
            flush=True,
        )
        return encoded, prompt_tokens, compressed

    def complete(self, payload: dict[str, Any]) -> str:
        messages = payload.get("messages") or []
        with self.dispatch_lock:
            worker = self.workers[self.next_worker]
            self.next_worker = (self.next_worker + 1) % len(self.workers)
        max_tokens = int(payload.get("max_completion_tokens") or payload.get("max_tokens") or 512)
        max_tokens = max(1, min(max_tokens, 2048))
        encoded, prompt_tokens, compressed_messages = self._encode_with_adaptive_compression(
            messages,
            max_tokens,
        )
        if prompt_tokens + max_tokens > self.max_model_len:
            raise ContextLengthError(
                f"prompt has {prompt_tokens} tokens and requests {max_tokens} output "
                f"tokens, exceeding max_model_len={self.max_model_len}; "
                f"compressed_messages={compressed_messages}"
            )
        encoded = encoded.to(worker.input_device)
        temperature = float(payload.get("temperature") or 0.0)
        top_p = float(payload.get("top_p") or 0.95)
        top_k = int(payload.get("top_k") or 20)
        eos_token_ids = [self.tokenizer.eos_token_id]
        for token in ("<|eot_id|>", "<|im_end|>"):
            token_id = self.tokenizer.convert_tokens_to_ids(token)
            if token_id is not None and token_id >= 0 and token_id not in eos_token_ids:
                eos_token_ids.append(token_id)
        kwargs: dict[str, Any] = {
            "max_new_tokens": max_tokens,
            "pad_token_id": self.tokenizer.eos_token_id,
            "eos_token_id": eos_token_ids,
            "repetition_penalty": 1.03,
            "do_sample": temperature > 0,
        }
        if temperature > 0:
            kwargs.update(temperature=temperature, top_p=top_p, top_k=top_k)
        with worker.lock, torch.inference_mode():
            output = worker.model.generate(**encoded, **kwargs)
        new_tokens = output[0, encoded["input_ids"].shape[1] :]
        return self.tokenizer.decode(new_tokens, skip_special_tokens=True).strip()


class Handler(BaseHTTPRequestHandler):
    state: State

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {fmt % args}", flush=True)

    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": self.state.model_id, "object": "model", "owned_by": "local"}]})
            return
        self._send(404, {"error": {"message": "not found", "type": "not_found"}})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": {"message": "not found", "type": "not_found"}})
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length))
            content = self.state.complete(payload)
            created = int(time.time())
            self._send(200, {
                "id": f"chatcmpl-local-{created}",
                "object": "chat.completion",
                "created": created,
                "model": self.state.model_id,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })
        except Exception as exc:
            status = 400 if isinstance(exc, ContextLengthError) else 500
            self._send(status, {"error": {"message": str(exc), "type": type(exc).__name__}})


def parse_device_groups(raw_groups: str, raw_devices: str, fallback: str) -> list[list[str]]:
    if raw_groups.strip():
        groups = [
            [device.strip() for device in group.split(",") if device.strip()]
            for group in raw_groups.split(";")
            if group.strip()
        ]
    else:
        devices = [device.strip() for device in raw_devices.split(",") if device.strip()]
        groups = [[device] for device in devices]
    return groups or [[fallback]]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--devices", default="")
    parser.add_argument("--device-groups", default="")
    parser.add_argument("--max-model-len", type=int, default=131072)
    parser.add_argument("--adaptive-observation-compression", action="store_true")
    args = parser.parse_args()
    device_groups = parse_device_groups(args.device_groups, args.devices, args.device)
    Handler.state = State(
        args.model,
        device_groups,
        args.max_model_len,
        args.adaptive_observation_compression,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"Chat model server ready on http://{args.host}:{args.port}/v1 "
        f"with device_groups={device_groups} "
        f"adaptive_observation_compression={args.adaptive_observation_compression}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
