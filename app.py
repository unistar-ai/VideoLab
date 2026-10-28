#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""VideoLab —— MiniMax H3 本地视频工作台。

用法：双击 启动_VideoLab.bat
调试：python app.py --no-window [--comfy-home <ComfyUI 目录>]
"""
from __future__ import annotations

import json
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "VideoLab"


def _resolve_comfy_home() -> str:
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--comfy-home":
            if i + 1 >= len(argv):
                raise SystemExit("--comfy-home 后面要跟 ComfyUI 目录")
            return os.path.abspath(argv[i + 1])
        if arg.startswith("--comfy-home="):
            return os.path.abspath(arg.split("=", 1)[1])
    env = os.environ.get("VIDEOLAB_COMFY_HOME")
    if env:
        return os.path.abspath(env)
    return os.path.abspath(os.path.join(os.path.dirname(APP_DIR), "ComfyUI_windows_portable"))


COMFY_HOME = _resolve_comfy_home()


def _comfy_root() -> str:
    nested = os.path.join(COMFY_HOME, "ComfyUI")
    return nested if os.path.isfile(os.path.join(nested, "main.py")) else COMFY_HOME


COMFY_ROOT = _comfy_root()
COMFY_MAIN = os.path.join(COMFY_ROOT, "main.py")
COMFY_PY = next((p for p in (os.path.join(COMFY_HOME, "python_embeded", "python.exe"),
                             sys.executable) if os.path.isfile(p)), sys.executable)
MODELS_DIR = os.path.join(COMFY_ROOT, "models")
COMFY_IN = os.path.join(COMFY_ROOT, "input")
COMFY_OUT = os.path.join(COMFY_ROOT, "output")
COMFY_LOG = os.path.join(APP_DIR, "comfyui.log")
APP_LOG = os.path.join(APP_DIR, "videolab.log")
COMFY_PORT = 8189
COMFY = "http://127.0.0.1:%d" % COMFY_PORT
UI_HOST, UI_PORT = "127.0.0.1", 8766
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

MODEL_FILES = {
    "unet": ("diffusion_models", "minimax_h3_fl2va_pruned_int8_convrot.safetensors"),
    "ref_unet": ("diffusion_models", "minimax_h3_ref2va_pruned_int8_convrot.safetensors"),
    "clip": ("text_encoders", "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"),
    "vae": ("vae", "minimax_h3_video_vae_fp16.safetensors"),
    "audio_vae": ("vae", "minimax_h3_audio_vae_fp32.safetensors"),
}
TURBO_LORAS = {
    "fl2va": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
    "ref2va": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
}
HISTORY_FILE = os.path.join(APP_DIR, "history.json")
HISTORY_MAX = 100
OUTPUT_PREFIX = "video/VideoLab"
UI_HTML = os.path.join(APP_DIR, "ui.html")
VIDEO_EXTS = (".mp4", ".webm")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
MIME_TYPES = {
    ".mp4": "video/mp4", ".webm": "video/webm",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
VIDEO_TIMEOUT = 4 * 60 * 60

STATE = {"owned": False, "proc": None, "ready": False, "error": None, "models": {}}
LOCK = threading.Lock()
JOBS: dict[str, dict] = {}
LAST_BEAT = time.time()
SHUTDOWN = threading.Event()


def log(msg: str) -> None:
    line = time.strftime("%H:%M:%S") + "  " + msg
    print(line, flush=True)
    try:
        with open(APP_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d ") + line + "\n")
    except OSError:
        pass


def http_json(url: str, payload=None, timeout=30):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read()
        return json.loads(body.decode("utf-8")) if body else {}


def comfy_stats(timeout=2.0):
    try:
        return http_json(COMFY + "/system_stats", timeout=timeout)
    except Exception:
        return None


def vram_mib():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW).stdout.strip()
        used, total = [int(x) for x in out.split(",")[:2]]
        return used, total
    except Exception:
        return None


def safe_name(name: str) -> str:
    base = os.path.basename(name or "")
    base = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fa5]", "_", base)
    return base[:120] or "frame.png"


def safe_relative(name: str) -> str | None:
    value = (name or "").replace("\\", "/").strip("/")
    if not value or os.path.isabs(value) or re.match(r"^[A-Za-z]:", value):
        return None
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return "/".join(safe_name(part) for part in parts)


def missing_models() -> list[str]:
    missing = []
    for folder, name in MODEL_FILES.values():
        if not os.path.isfile(os.path.join(MODELS_DIR, folder, name)):
            missing.append(folder + "/" + name)
    return missing


def scan_models() -> None:
    with LOCK:
        STATE["models"] = {
            key: name if os.path.isfile(os.path.join(MODELS_DIR, folder, name)) else ""
            for key, (folder, name) in MODEL_FILES.items()
        }


def model_info() -> dict:
    missing = missing_models()
    return {
        "name": "MiniMax H3",
        "installed": not missing,
        "missing": missing,
        "files": {key: folder + "/" + name for key, (folder, name) in MODEL_FILES.items()},
    }


def turbo_info() -> dict:
    return {
        key: {
            "installed": os.path.isfile(os.path.join(MODELS_DIR, "loras", name)),
            "file": "loras/" + name,
        }
        for key, name in TURBO_LORAS.items()
    }


def boot_comfy() -> None:
    try:
        if comfy_stats():
            log("ComfyUI 8189 已在运行，复用")
            STATE["ready"] = True
            scan_models()
            return
        if not os.path.isfile(COMFY_MAIN):
            raise RuntimeError(
                "找不到 ComfyUI（预期在 %s）。用 --comfy-home 参数或 VIDEOLAB_COMFY_HOME 环境变量指定。"
                % COMFY_MAIN)
        log("启动 ComfyUI 后端 … (%s)" % COMFY_HOME)
        logf = open(COMFY_LOG, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            [COMFY_PY, "-s", COMFY_MAIN, "--port", str(COMFY_PORT),
             "--listen", "127.0.0.1", "--disable-auto-launch", "--reserve-vram", "0.7"],
            cwd=COMFY_HOME, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW)
        STATE["proc"] = proc
        STATE["owned"] = True
        for _ in range(240):
            if proc.poll() is not None:
                raise RuntimeError("ComfyUI 进程已退出，详见 comfyui.log")
            if comfy_stats(timeout=1.0):
                STATE["ready"] = True
                log("ComfyUI 就绪")
                scan_models()
                return
            time.sleep(1)
        raise RuntimeError("ComfyUI 启动超时（240 秒）")
    except Exception as exc:
        STATE["error"] = str(exc)
        log("后端启动失败: %s" % exc)


def shutdown_comfy() -> None:
    proc = STATE.get("proc")
    if STATE.get("owned") and proc and proc.poll() is None:
        log("关闭 VideoLab 启动的 ComfyUI")
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def duration_frames(duration: int) -> int:
    frames = max(5, int(duration) * 24)
    return frames + (5 - frames % 17) % 17


def build_graph(prompt: str, width: int, height: int, duration: int,
                steps: int, seed: int, first_frame: str = "",
                last_frame: str = "", mode: str = "t2v",
                reference_images: list[str] | None = None,
                turbo: bool = False) -> dict:
    r2v = mode == "r2v"
    model = ["15", 0] if turbo else ["1", 0]
    nodes = {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": MODEL_FILES["ref_unet" if r2v else "unet"][1],
            "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": MODEL_FILES["clip"][1], "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MODEL_FILES["vae"][1]}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": MODEL_FILES["audio_vae"][1]}},
        "5": {"class_type": "MiniMaxH3ReferenceToVideo" if r2v else "MiniMaxH3ImageToVideo",
              "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
            "width": width, "height": height, "length": duration_frames(duration)}},
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "7": {"class_type": "BasicGuider", "inputs": {
            "model": model, "conditioning": ["5", 0]}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "9": {"class_type": "BasicScheduler", "inputs": {
            "model": model, "scheduler": "simple", "steps": steps, "denoise": 1}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["6", 0], "guider": ["7", 0], "sampler": ["8", 0],
            "sigmas": ["9", 0], "latent_image": ["5", 1]}},
        "11": {"class_type": "VAEDecode", "inputs": {
            "samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "VAEDecodeAudio", "inputs": {
            "samples": ["10", 0], "vae": ["4", 0]}},
        "13": {"class_type": "CreateVideo", "inputs": {
            "images": ["11", 0], "audio": ["12", 0], "fps": 24,
            "bit_depth": 8, "color_space": "sRGB"}},
        "14": {"class_type": "SaveVideo", "inputs": {
            "video": ["13", 0], "filename_prefix": OUTPUT_PREFIX,
            "format": "mp4", "codec": {"codec": "h264"}}},
    }
    if turbo:
        nodes["15"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["1", 0],
            "lora_name": TURBO_LORAS["ref2va" if r2v else "fl2va"],
            "strength_model": 1.0}}
    if r2v:
        nodes["5"]["inputs"].update({
            "audio_vae": ["4", 0], "ref_image_size": "match"})
        for index, filename in enumerate(reference_images or []):
            nid = str(20 + index)
            nodes[nid] = {"class_type": "LoadImage", "inputs": {"image": filename}}
            nodes["5"]["inputs"]["ref_images.ref_image_%d" % index] = [nid, 0]
    elif mode == "i2v":
        for nid, key, filename in (("20", "first_frame", first_frame),
                                   ("21", "last_frame", last_frame)):
            if filename:
                nodes[nid] = {"class_type": "LoadImage", "inputs": {"image": filename}}
                nodes["5"]["inputs"][key] = [nid, 0]
    return nodes


def watch_vram(job: dict) -> None:
    while not job.get("done"):
        value = vram_mib()
        if value:
            job["peak"] = max(job.get("peak") or 0, value[0])
        time.sleep(1.0)


class Cancelled(Exception):
    pass


def submit_one(graph: dict) -> str:
    result = http_json(COMFY + "/prompt", {"prompt": graph, "client_id": "videolab"}, timeout=60)
    prompt_id = result.get("prompt_id")
    if not prompt_id:
        raise RuntimeError("提交失败: %s" % json.dumps(result, ensure_ascii=False)[:400])
    return prompt_id


def comfy_interrupt() -> bool:
    ok = False
    for path, payload in (("/interrupt", {}), ("/queue", {"clear": True})):
        try:
            http_json(COMFY + path, payload, timeout=10)
            ok = True
        except Exception as exc:
            log("停止请求 %s 失败：%s" % (path, exc))
    return ok


def _video_output(entry: dict) -> dict | None:
    for node in (entry.get("outputs") or {}).values():
        for key in ("videos", "images"):
            for video in node.get(key, []):
                return {"filename": video.get("filename"),
                        "subfolder": video.get("subfolder", "")}
    return None


def peek_output(prompt_id: str):
    try:
        entry = http_json(COMFY + "/history/%s" % prompt_id, timeout=5).get(prompt_id) or {}
        return _video_output(entry)
    except Exception:
        return None


def wait_one(prompt_id: str, timeout=VIDEO_TIMEOUT, cancel=None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        if cancel is not None and cancel.is_set():
            done = peek_output(prompt_id)
            if done:
                return done
            raise Cancelled()
        entry = http_json(COMFY + "/history/%s" % prompt_id, timeout=10).get(prompt_id)
        if not entry:
            continue
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            raise RuntimeError(_error_text(status.get("messages") or []))
        output = _video_output(entry)
        if output:
            return output
        if status.get("completed"):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            raise RuntimeError("任务结束但没有输出视频")
    raise RuntimeError("等待视频超时")


def run_job(job_id: str, opts: dict) -> None:
    job = JOBS[job_id]
    cancel = job["cancel"]
    seeds = opts["seeds"]
    try:
        for index, seed in enumerate(seeds):
            if cancel.is_set():
                job["cancelled"] = True
                break
            job["current"] = index + 1
            graph = build_graph(opts["prompt"], opts["width"], opts["height"],
                                opts["duration"], opts["steps"], seed,
                                opts["first_frame"], opts["last_frame"],
                                opts["mode"], opts["reference_images"], opts["turbo"])
            prompt_id = submit_one(graph)
            job["prompt_id"] = prompt_id
            if cancel.is_set():
                comfy_interrupt()
            output = wait_one(prompt_id, cancel=cancel)
            output["seed"] = seed
            with LOCK:
                job["videos"].append(output)
    except Cancelled:
        job["cancelled"] = True
        log("任务 %s 已停止（已出 %d/%d 个视频）"
            % (job_id, len(job["videos"]), job.get("total", 1)))
    except Exception as exc:
        job["error"] = _friendly(exc)
    finally:
        if job["videos"]:
            try:
                first = job["videos"][0]
                add_history({
                    "mode": opts["mode"], "turbo": opts["turbo"],
                    "prompt": opts["prompt"], "width": opts["width"], "height": opts["height"],
                    "size": "%dx%d" % (opts["width"], opts["height"]),
                    "duration": opts["duration"], "frames": duration_frames(opts["duration"]),
                    "steps": opts["steps"], "batch": job.get("total", 1),
                    "seed": seeds[0], "first_frame": opts["first_frame"],
                    "last_frame": opts["last_frame"],
                    "reference_images": opts["reference_images"], "file": first["filename"],
                    "subfolder": first.get("subfolder", ""),
                    "elapsed": round(time.time() - job["start"], 1),
                })
            except Exception as exc:
                log("写历史失败：%s" % exc)
        job["elapsed"] = time.time() - job["start"]
        job["done"] = True


def _error_text(messages) -> str:
    for message in messages:
        try:
            if message[0] == "execution_error":
                return str(message[1].get("exception_message") or message[1])[:600]
        except Exception:
            continue
    return "执行出错"


def _friendly(exc: Exception) -> str:
    text, low = str(exc), str(exc).lower()
    if "out of memory" in low or "allocation" in low:
        return "显存不足。关闭其他占显存的程序，或降低分辨率和时长。"
    if "not in list" in low or "value not in" in low:
        return "模型文件缺失或名称不匹配：" + text[:300]
    return text[:600]


def window_alive(title: str) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lp):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if title.lower() in buf.value.lower():
                        found.append(hwnd)
            return True

        user32.EnumWindows(callback_type(callback), 0)
        return bool(found)
    except Exception:
        return True


def watchdog(title: str) -> None:
    time.sleep(5)
    missing = 0
    while not SHUTDOWN.is_set():
        time.sleep(2)
        gone = False
        if title:
            if window_alive(title):
                missing = 0
            else:
                missing += 1
                gone = missing >= 3
        if gone and time.time() - LAST_BEAT > 15:
            log("窗口已关闭且无心跳，退出")
            request_shutdown()
            return


def request_shutdown() -> None:
    SHUTDOWN.set()


def touch_heartbeat() -> None:
    global LAST_BEAT
    LAST_BEAT = time.time()


def _output_path(name: str) -> str | None:
    root = os.path.abspath(COMFY_OUT)
    relative = safe_relative(name)
    if not relative:
        return None
    path = os.path.abspath(os.path.join(root, *relative.split("/")))
    return path if path.startswith(root + os.sep) else None


def list_outputs(limit: int = 60, offset: int = 0, query: str = "") -> dict:
    root = os.path.abspath(COMFY_OUT)
    query = query.lower()
    files = []
    for folder, _dirs, names in os.walk(root):
        for name in names:
            if not name.lower().endswith(VIDEO_EXTS):
                continue
            path = os.path.join(folder, name)
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if query and query not in relative.lower():
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            files.append({"name": relative, "size": stat.st_size, "mtime": int(stat.st_mtime)})
    files.sort(key=lambda item: item["mtime"], reverse=True)
    return {"total": len(files), "items": files[offset:offset + limit]}


def load_history() -> list:
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    for index, item in enumerate(data):
        if isinstance(item, dict) and not item.get("id"):
            item["id"] = "%s-%d" % (item.get("t", 0), index)
    return [item for item in data if isinstance(item, dict)][:HISTORY_MAX]


def save_history(items: list) -> None:
    try:
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items[:HISTORY_MAX], f, ensure_ascii=False, indent=1)
        os.replace(tmp, HISTORY_FILE)
    except Exception as exc:
        log("写历史失败: %s" % exc)


def add_history(entry: dict) -> list:
    with LOCK:
        items = load_history()
        entry = dict(entry)
        entry["t"] = int(time.time())
        entry["id"] = uuid.uuid4().hex[:12]
        items.insert(0, entry)
        save_history(items)
        return items[:HISTORY_MAX]


def delete_history(ids: list) -> list:
    wanted = {str(item) for item in (ids or [])}
    with LOCK:
        items = [item for item in load_history() if str(item.get("id")) not in wanted]
        save_history(items)
        return items[:HISTORY_MAX]


def clear_history() -> None:
    with LOCK:
        save_history([])


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoLab/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body: bytes, ctype: str, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def _send_file(self, path: str):
        ext = os.path.splitext(path)[1].lower()
        ctype = MIME_TYPES.get(ext, "application/octet-stream")
        size = os.path.getsize(path)
        start, end = 0, size - 1
        code = 200
        range_header = self.headers.get("Range") or ""
        match = re.match(r"bytes=(\d*)-(\d*)$", range_header)
        if match and size:
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            elif last:
                start = max(0, size - int(last))
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        length = max(0, end - start + 1)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (OSError, BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            try:
                with open(UI_HTML, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, b"ui.html missing", "text/plain")
        if path == "/api/library":
            try:
                limit = max(1, min(200, int((query.get("limit") or ["60"])[0])))
                offset = max(0, int((query.get("offset") or ["0"])[0]))
            except ValueError:
                return self._json(400, {"error": "分页参数无效"})
            text = (query.get("q") or [""])[0].strip()
            return self._json(200, list_outputs(limit, offset, text))
        if path == "/api/history":
            return self._json(200, {"items": load_history()})
        if path == "/api/health":
            scan_models()
            value = vram_mib()
            model = model_info()
            return self._json(200, {
                "ready": STATE["ready"], "error": STATE["error"], "app": APP_NAME,
                "comfy_port": COMFY_PORT, "models": STATE["models"], "model": model,
                "turbo": turbo_info(),
                "vram": {"used": value[0], "total": value[1]} if value else None})
        if path == "/api/progress":
            job = JOBS.get((query.get("id") or [""])[0])
            if not job:
                return self._json(404, {"error": "unknown job"})
            return self._json(200, {
                "done": bool(job.get("done")), "cancelled": bool(job.get("cancelled")),
                "videos": job.get("videos", []), "total": job.get("total", 1),
                "current": job.get("current", 0),
                "elapsed": round(job.get("elapsed") or (time.time() - job["start"]), 1),
                "peak_mib": job.get("peak"), "error": job.get("error")})
        if path == "/api/view":
            filename = (query.get("filename") or [""])[0]
            subfolder = (query.get("subfolder") or [""])[0]
            kind = (query.get("type") or ["output"])[0]
            if kind not in ("output", "input", "temp"):
                kind = "output"
            base = {"output": COMFY_OUT, "input": COMFY_IN,
                    "temp": os.path.join(COMFY_ROOT, "temp")}[kind]
            root = os.path.abspath(base)
            relative = safe_relative(filename)
            if relative and subfolder:
                sub = safe_relative(subfolder)
                relative = sub + "/" + relative if sub else None
            target = os.path.abspath(os.path.join(root, *(relative or "").split("/")))
            if relative and target.startswith(root + os.sep) and os.path.isfile(target):
                return self._send_file(target)
            return self._json(404, {"error": "文件不存在"})
        if path == "/api/beat":
            touch_heartbeat()
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/beat":
            touch_heartbeat()
            return self._json(200, {"ok": True})
        if path == "/api/upload":
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return self._json(400, {"error": "空文件"})
            if length > 100 * 1024 * 1024:
                return self._json(413, {"error": "图片不能超过 100 MB"})
            raw = self.rfile.read(length)
            name = safe_name(urllib.parse.unquote(self.headers.get("X-Filename") or "frame.png"))
            stem, ext = os.path.splitext(name)
            if ext.lower() not in IMAGE_EXTS:
                ext = ".png"
            final = "%s_%d%s" % (stem[:60], int(time.time() * 1000), ext)
            os.makedirs(COMFY_IN, exist_ok=True)
            with open(os.path.join(COMFY_IN, final), "wb") as f:
                f.write(raw)
            return self._json(200, {"name": final})
        try:
            body = self._body_json()
        except Exception:
            return self._json(400, {"error": "bad json"})
        if path == "/api/history/delete":
            ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
            if not ids:
                return self._json(400, {"error": "缺少 id"})
            items = delete_history(ids)
            return self._json(200, {"items": items, "removed": len(ids)})
        if path == "/api/history/clear":
            clear_history()
            return self._json(200, {"items": []})
        if path == "/api/library/delete":
            removed, failed = [], []
            for name in body.get("names") or []:
                target = _output_path(name)
                if not target or not os.path.isfile(target) or not target.lower().endswith(VIDEO_EXTS):
                    failed.append(name)
                    continue
                try:
                    os.remove(target)
                    removed.append(name)
                except OSError:
                    failed.append(name)
            return self._json(200, {"removed": removed, "failed": failed})
        if path == "/api/open-folder":
            name = body.get("name") or ""
            target = _output_path(name) if name else None
            try:
                if target and os.path.isfile(target):
                    subprocess.Popen(["explorer.exe", "/select,", target],
                                     creationflags=CREATE_NO_WINDOW)
                else:
                    os.makedirs(COMFY_OUT, exist_ok=True)
                    subprocess.Popen(["explorer.exe", os.path.abspath(COMFY_OUT)],
                                     creationflags=CREATE_NO_WINDOW)
            except Exception as exc:
                return self._json(500, {"error": str(exc)})
            return self._json(200, {"ok": True})
        if path == "/api/generate":
            if not STATE["ready"]:
                return self._json(503, {"error": STATE["error"] or "ComfyUI 8189 还没就绪"})
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                return self._json(400, {"error": "提示词不能为空"})
            mode = body.get("mode") or "t2v"
            if mode not in ("t2v", "i2v", "r2v"):
                return self._json(400, {"error": "mode 必须是 t2v、i2v 或 r2v"})
            turbo = body.get("turbo", False)
            if type(turbo) is not bool:
                return self._json(400, {"error": "turbo 必须是布尔值"})
            reference_images = body.get("reference_images")
            if mode == "r2v":
                if not isinstance(reference_images, list) or not 1 <= len(reference_images) <= 9:
                    return self._json(400, {"error": "reference_images 必须包含 1–9 张参考图"})
                for index, name in enumerate(reference_images):
                    if not isinstance(name, str) or not name.strip():
                        return self._json(400, {"error": "第 %d 张参考图文件名无效" % (index + 1)})
                reference_images = [safe_name(name) for name in reference_images]
            else:
                reference_images = []
            scan_models()
            missing = missing_models()
            if missing:
                return self._json(400, {"error": "缺少必需模型：\n" + "\n".join(missing)})
            if turbo:
                lora = TURBO_LORAS["ref2va" if mode == "r2v" else "fl2va"]
                if not os.path.isfile(os.path.join(MODELS_DIR, "loras", lora)):
                    return self._json(400, {"error": "Turbo 模式缺少 LoRA：loras/" + lora})
            try:
                width = max(32, min(2048, int(body.get("width") or 864))) // 32 * 32
                height = max(32, min(2048, int(body.get("height") or 480))) // 32 * 32
                duration = max(5, min(15, int(body.get("duration") or 5)))
                steps = max(1, min(100, int(body.get("steps") or 20)))
                batch = max(1, min(4, int(body.get("batch") or 1)))
                seed = int(body.get("seed") or 0)
            except (TypeError, ValueError):
                return self._json(400, {"error": "生成参数无效"})
            first_frame = safe_name(body.get("first_frame") or "") if body.get("first_frame") else ""
            last_frame = safe_name(body.get("last_frame") or "") if body.get("last_frame") else ""
            max_seed = 2 ** 63 - 1
            if seed > 0:
                seeds = [(seed + index) % max_seed for index in range(batch)]
            else:
                seeds = [random.randint(1, max_seed) for _ in range(batch)]
            job_id = "%d%04d" % (int(time.time()), random.randint(0, 9999))
            JOBS[job_id] = {"start": time.time(), "done": False, "peak": 0,
                            "videos": [], "total": batch, "current": 0,
                            "cancel": threading.Event(), "cancelled": False}
            opts = {"prompt": prompt, "mode": mode, "turbo": turbo,
                    "width": width, "height": height,
                    "duration": duration, "steps": steps, "seeds": seeds,
                    "first_frame": first_frame, "last_frame": last_frame,
                    "reference_images": reference_images}
            threading.Thread(target=run_job, daemon=True, args=(job_id, opts)).start()
            threading.Thread(target=watch_vram, args=(JOBS[job_id],), daemon=True).start()
            return self._json(200, {"id": job_id, "seeds": seeds,
                                    "frames": duration_frames(duration)})
        if path == "/api/cancel":
            job = JOBS.get((body.get("id") or "").strip())
            if not job:
                return self._json(404, {"error": "任务不存在或已结束"})
            if job.get("done"):
                return self._json(200, {"ok": True, "done": True})
            job["cancel"].set()
            stopped = comfy_interrupt()
            return self._json(200, {"ok": True, "interrupted": stopped})
        if path == "/api/quit":
            self._json(200, {"ok": True})
            threading.Timer(0.3, request_shutdown).start()
            return
        self._json(404, {"error": "not found"})


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((UI_HOST, port))
            return True
        except OSError:
            return False


def launch_window(url: str) -> None:
    for exe in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if os.path.exists(exe):
            subprocess.Popen(
                [exe, "--app=" + url, "--window-size=1260,900",
                 "--user-data-dir=" + os.path.join(APP_DIR, "_window_profile"),
                 "--no-first-run", "--no-default-browser-check",
                 "--disable-features=Translate"], creationflags=CREATE_NO_WINDOW)
            return
    import webbrowser
    webbrowser.open(url)


def main() -> int:
    global UI_PORT, LAST_BEAT
    no_window = "--no-window" in sys.argv
    scan_models()
    threading.Thread(target=boot_comfy, daemon=True).start()
    while not port_free(UI_PORT):
        UI_PORT += 1
    server = None
    for _ in range(20):
        try:
            server = ThreadingHTTPServer((UI_HOST, UI_PORT), Handler)
            break
        except OSError:
            UI_PORT += 1
    if server is None:
        log("找不到可用端口，退出")
        return 1
    url = "http://%s:%d/" % (UI_HOST, UI_PORT)
    log("界面地址 %s" % url)
    LAST_BEAT = time.time()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if not no_window:
        launch_window(url)
        threading.Thread(target=watchdog, args=(APP_NAME,), daemon=True).start()
    log("运行中")
    try:
        while not SHUTDOWN.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    log("正在退出")
    server.shutdown()
    shutdown_comfy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
