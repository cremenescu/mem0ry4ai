#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Minimal Ollama wrapper (stdlib-only) for memory extraction/classification.

Small local model (default Qwen2.5-7B-Instruct), structured output via JSON schema,
low temperature. If Ollama is down -> ollama_up() returns False and callers degrade
to manual capture.
"""
import json
import os
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("MEM_LLM_MODEL", "qwen2.5:7b-instruct")


def ollama_up():
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def generate_json(system, prompt, schema, timeout=240):
    """Generate JSON constrained by a schema. Returns the parsed Python object.

    Raises RuntimeError if Ollama errors out or the JSON cannot be parsed.
    """
    body = json.dumps({
        "model": MODEL,
        "system": system,
        "prompt": prompt,
        "stream": False,
        "format": schema,
        "options": {"temperature": 0.1, "num_ctx": 16384},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate", data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(data["error"])
    resp = data.get("response", "").strip()
    try:
        return json.loads(resp)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"invalid JSON from the model: {e}\n{resp[:500]}")


if __name__ == "__main__":
    # self-test
    print("Ollama up:", ollama_up())
    if ollama_up():
        out = generate_json(
            "Answer with JSON only, matching the schema.",
            "List 2 colors.",
            {"type": "array", "items": {"type": "string"}},
        )
        print("test:", out)
