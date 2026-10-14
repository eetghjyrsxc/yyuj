#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手机桥 · 手机端服务（在 Termux 里跑）

一个进程干两件事：

  1. **只读文件服务**：目录列表 / `HEAD` 的 Last-Modified、Content-Length / 404 文案
     都照抄 v1 那套 `python -m http.server` 的输出，所以沙箱里的
     `运维工具/手机文件.sh` 一个字都不用改就能继续用。
  2. **白名单执行层**（`/api/*` JSON 接口）：往手机写文件、弹通知、打开网址或文件、
     拍照、执行 termux-* 命令。沙箱侧入口是 `运维工具/手机控制.sh`。

边界（全部由本文件兜底，不靠调用方自觉）
  · 只绑 127.0.0.1 —— 同 WiFi 的别人访问不到，能连的只有手机上的本机进程
  · 写：只落白名单目录（默认 Download / DCIM / Pictures / Documents / Movies / Music），
        默认**只新建**（同名已存在就 409），**没有删除、没有改名**接口
  · 跑：只认白名单命令（termux-* 加少数只读系统命令），传的是 argv 数组、**不过 shell**，
        所以 `|` `>` `*` 反引号这些一律无效
  · 危险命令（短信、通讯录、拨号、通话记录）默认关闭，要 `--allow-risky` 显式打开

用法（Termux 里）：
    python ~/phone_server.py                         # 默认 8899 / /storage/emulated/0
    python ~/phone_server.py --allow-risky           # 放开短信/拨号等危险命令（慎用）
    python ~/phone_server.py --overwrite             # 允许覆盖同名文件（默认只新建）

接口（沙箱侧不直接手敲，看 运维工具/手机控制.sh）
    GET  /api/status                     桥与执行器能力清单
    GET  /api/probe                      真跑一次 termux-battery-status，验证 Termux:API 通不通
    POST /api/write?path=&overwrite=&scan=   裸字节写入（body 就是文件内容）
    POST /api/notify   {"title","content","id"}
    POST /api/open     {"target"}（网址）或 {"path"}（手机上的文件）
    POST /api/photo    {"dir","camera"}  拍一张，返回落点
    POST /api/pick     {"dir","name"}    手机上弹选择器，收一个文件进来
    POST /api/scan     {"path"}          让相册/媒体库看见
    POST /api/run      {"argv":[...],"timeout"}   白名单命令，返回 stdout/stderr/退出码
"""

import argparse
import html
import json
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.0"
DEFAULT_WRITE_ROOTS = ("Download", "DCIM", "Pictures", "Documents", "Movies", "Music")

# 危险命令：会发短信、读到通讯录/通话记录、能拨号 —— 默认不给
RISKY = {
    "termux-sms-send", "termux-sms-list", "termux-contact-list",
    "termux-telephony-call", "termux-call-log",
}
# 白名单里额外的只读系统命令（getprop 之类，用来报机型和探测）
SAFE_EXTRA = {"getprop", "df", "uname", "id", "uptime", "date", "cat"}
CMD_RE = re.compile(r"^termux-[a-z0-9]+(-[a-z0-9]+)*$")

# 用来给沙箱报「装了哪些」
CANDIDATES = [
    "termux-battery-status", "termux-brightness", "termux-camera-info", "termux-camera-photo",
    "termux-clipboard-get", "termux-clipboard-set", "termux-dialog", "termux-download",
    "termux-fingerprint", "termux-job-scheduler", "termux-location", "termux-media-player",
    "termux-media-scan", "termux-microphone-record", "termux-notification", "termux-open",
    "termux-open-url", "termux-sensor", "termux-setup-storage", "termux-share",
    "termux-storage-get", "termux-toast", "termux-torch", "termux-tts-speak",
    "termux-vibrate", "termux-volume", "termux-wake-lock", "termux-wifi-connectioninfo",
]

# ── 运行期配置（main 里填）────────────────────────────────────────────────
ROOT = "/storage/emulated/0"
WRITE_ROOTS = list(DEFAULT_WRITE_ROOTS)
ALLOW_RISKY = False
ALLOW_OVERWRITE = False
MAX_WRITE = 512 * 1024 * 1024  # 单次写入上限 512 MB
MAX_OUTPUT = 1 * 1024 * 1024   # 命令输出回传上限 1 MB
STARTED = time.time()


class BridgeError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# ── 路径与命令的两道门 ───────────────────────────────────────────────────

def resolve(raw, need_write=False):
    """把客户端给的相对路径规整成 Root 下的绝对路径；越界、`..`、非法名一律拒。"""
    rel = urllib.parse.unquote(raw or "").strip()
    if "\x00" in rel:
        raise BridgeError(400, "路径里有 NUL 字节")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise BridgeError(400, "只接受相对路径（相对于手机共享存储根目录）")
    norm = posixpath.normpath(rel.replace("\\", "/"))
    if norm in ("", ".", "..") or norm.startswith("../"):
        raise BridgeError(400, "路径不合法")
    if any(seg in ("", "..") for seg in norm.split("/")):
        raise BridgeError(400, "路径里不许出现 .. 或空段")

    if need_write:
        top = norm.split("/")[0]
        if top not in WRITE_ROOTS:
            raise BridgeError(403, "写目录不在白名单：%s（允许：%s）" % (top, ", ".join(WRITE_ROOTS)))

    abs_path = os.path.join(ROOT, norm)
    real_root = os.path.realpath(ROOT)
    real = os.path.realpath(abs_path)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise BridgeError(403, "解析后跑到根目录外面去了")
    return abs_path, norm


def check_argv(argv):
    """白名单校验，返回可直接交给 subprocess 的 argv（argv[0] 换成绝对路径）。"""
    if not isinstance(argv, list) or not argv or len(argv) > 32:
        raise BridgeError(400, "argv 必须是 1~32 个元素的数组")
    for a in argv:
        if not isinstance(a, str) or "\x00" in a or len(a) > 4096:
            raise BridgeError(400, "参数不合法")
    exe = os.path.basename(argv[0])
    if not (CMD_RE.match(exe) or exe in SAFE_EXTRA):
        raise BridgeError(403, "命令不在白名单：%s" % exe)
    if exe in RISKY and not ALLOW_RISKY:
        raise BridgeError(403, "%s 是危险命令，默认关闭（要开：给 phone_server.py 加 --allow-risky）" % exe)
    full = shutil.which(exe)
    if not full:
        raise BridgeError(501, "没找到 %s —— Termux:API 装了吗？（pkg install termux-api）" % exe)
    return [full] + [str(a) for a in argv[1:]]


def run_cmd(argv, timeout=30, stdin=None):
    """跑一条白名单命令，返回 (退出码, stdout, stderr)。"""
    cmd = check_argv(argv)
    t = max(1, min(int(timeout or 30), 600))
    try:
        p = subprocess.run(
            cmd, capture_output=True, timeout=t,
            input=stdin.encode("utf-8") if stdin is not None else None,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        raise BridgeError(504, "%s 超时（%ds）——如果是 termux-* 命令，多半是 Termux:API 没响应" % (cmd[0], t))
    except FileNotFoundError:
        raise BridgeError(501, "找不到可执行文件：%s" % cmd[0])
    out = p.stdout.decode("utf-8", "replace")[:MAX_OUTPUT]
    err = p.stderr.decode("utf-8", "replace")[:MAX_OUTPUT]
    return p.returncode, out, err


def media_scan(abs_path):
    if not shutil.which("termux-media-scan"):
        return False
    try:
        subprocess.run(["termux-media-scan", abs_path], capture_output=True, timeout=20)
        return True
    except Exception:
        return False


def getprop(key):
    exe = shutil.which("getprop") or "/system/bin/getprop"
    try:
        p = subprocess.run([exe, key], capture_output=True, timeout=5)
        return p.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""


# ── HTTP ────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "phone-bridge/" + VERSION
    protocol_version = "HTTP/1.1"

    # ---- 小工具 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _drain_or_close(self):
        """出错时处理掉还没读的请求体。

        小体量读掉就完事；体量大或读不动就直接断连接——
        否则 keep-alive 上剩下的字节会被当成下一条请求行（实测 curl 会撞上）。
        注意：**已经读过的不能再读**，否则会死等一个永远不来的 body（实测会挂住）。
        """
        if self.command != "POST" or getattr(self, "_body_read", False):
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if 0 < n <= 262144:
            try:
                self.rfile.read(n)
            except Exception:
                self.close_connection = True
        elif n > 262144:
            self.close_connection = True

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, indent=2))

    def _err_page(self, code, message):
        """照抄 http.server 的报错页，沙箱侧的老客户端靠 <title> 和 Message: 认它。"""
        body = (
            '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN" "http://www.w3.org/TR/html4/strict.dtd">\n'
            "<html><head>\n"
            '<meta http-equiv="Content-Type" content="text/html;charset=utf-8">\n'
            "<title>Error response</title>\n</head><body>\n"
            "<h1>Error response</h1>\n"
            "<p>Error code: %d</p>\n"
            "<p>Message: %s</p>\n"
            "</body></html>\n" % (code, html.escape(message, quote=False))
        )
        self._send(code, body, "text/html; charset=utf-8")

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
        sys.stdout.flush()

    def _body_json(self, limit=1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise BridgeError(411, "缺 Content-Length")
        if n > limit:
            raise BridgeError(413, "请求体太大")
        try:
            raw = self.rfile.read(n)
            self._body_read = True
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise BridgeError(400, "JSON 解析失败：%s" % e)

    def _query(self):
        qs = urllib.parse.urlparse(self.path).query
        return urllib.parse.parse_qs(qs, keep_blank_values=True)

    # ---- 路由 ----
    def do_GET(self):
        self._route(get=True)

    def do_HEAD(self):
        self._route(get=True)

    def do_POST(self):
        self._route(get=False)

    def _route(self, get):
        path = urllib.parse.urlparse(self.path).path
        self._body_read = False
        try:
            if path.startswith("/api/"):
                self._api(path, get)
            elif get:
                self._serve_file(path)
            else:
                raise BridgeError(405, "文件服务只支持 GET/HEAD")
        except BridgeError as e:
            self._fail(path, e.code, e.msg)
        except BrokenPipeError:
            pass
        except Exception as e:  # 兜底：别让一个坏请求把服务带崩
            self._fail(path, 500, "服务端异常：%r" % e)

    def _fail(self, path, code, msg):
        """出错回包。客户端提前跑了（Broken pipe）不算事，别弄得满屏 traceback。"""
        self._drain_or_close()
        try:
            if path.startswith("/api/"):
                self._json(code, {"ok": False, "error": msg, "code": code})
            else:
                self._err_page(code, msg)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- 只读文件服务（格式与 v1 兼容）----
    def _serve_file(self, path):
        rel = urllib.parse.unquote(path).strip("/")
        if not rel:
            return self._listing(ROOT, "/")
        abs_path, _ = resolve(rel)
        if os.path.isdir(abs_path):
            return self._listing(abs_path, "/" + rel)
        if not os.path.isfile(abs_path):
            raise BridgeError(404, "File not found.")
        st = os.stat(abs_path)
        ctype = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(st.st_size))
        self.send_header("Last-Modified", formatdate(st.st_mtime, usegmt=True))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(abs_path, "rb") as f:
            while True:
                chunk = f.read(262144)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _listing(self, abs_dir, url_path):
        try:
            names = sorted(os.listdir(abs_dir))
        except PermissionError:
            raise BridgeError(403, "Permission denied.")
        rows = []
        if url_path.strip("/"):
            parent = "/" + posixpath.dirname(url_path.strip("/")).strip("/")
            parent = parent if parent.endswith("/") else parent + "/"
            rows.append('<li><a href="%s">../</a></li>' % html.escape(parent, quote=True))
        for name in names:
            is_dir = os.path.isdir(os.path.join(abs_dir, name))
            disp = name + ("/" if is_dir else "")
            href = urllib.parse.quote(name) + ("/" if is_dir else "")
            rows.append('<li><a href="%s">%s</a></li>' % (html.escape(href, quote=True), html.escape(disp)))
        title = "Directory listing for %s" % urllib.parse.unquote(url_path)
        body = (
            "<!DOCTYPE HTML>\n<html>\n<head>\n"
            '<meta charset="utf-8">\n<title>%s</title>\n</head>\n<body>\n'
            "<h1>%s</h1>\n<hr>\n<ul>\n%s\n</ul>\n<hr>\n</body>\n</html>\n"
            % (html.escape(title), html.escape(title), "\n".join(rows))
        )
        self._send(200, body, "text/html; charset=utf-8")

    # ---- /api/* ----
    def _api(self, path, get):
        if path == "/api/status" and get:
            return self._json(200, self._status())
        if path == "/api/probe" and get:
            return self._json(200, self._probe())
        if path == "/api/write" and not get:
            return self._write()
        if not get:
            body = self._body_json()
            if path == "/api/notify":
                return self._notify(body)
            if path == "/api/open":
                return self._open(body)
            if path == "/api/photo":
                return self._photo(body)
            if path == "/api/scan":
                return self._scan(body)
            if path == "/api/pick":
                return self._pick(body)
            if path == "/api/run":
                return self._run(body)
        raise BridgeError(404, "没有这个接口：%s" % path)

    def _status(self):
        have = [c for c in CANDIDATES if shutil.which(c)]
        miss = [c for c in CANDIDATES if not shutil.which(c)]
        return {
            "ok": True,
            "name": "手机桥",
            "version": VERSION,
            "kind": "bridge2",
            "root": ROOT,
            "port": self.server.server_address[1],
            "write_roots": WRITE_ROOTS,
            "allow_overwrite": ALLOW_OVERWRITE,
            "allow_risky": ALLOW_RISKY,
            "risky_blocked": sorted(RISKY),
            "uptime_s": int(time.time() - STARTED),
            "commands": {"available": have, "missing": miss},
            "termux_api_ready": shutil.which("termux-battery-status") is not None,
            "device": {
                "model": getprop("ro.product.model"),
                "manufacturer": getprop("ro.product.manufacturer"),
                "android": getprop("ro.build.version.release"),
                "sdk": getprop("ro.build.version.sdk"),
            },
        }

    def _probe(self):
        t0 = time.time()
        code, out, err = run_cmd(["termux-battery-status"], timeout=12)
        dt = int((time.time() - t0) * 1000)
        info = None
        try:
            info = json.loads(out)
        except Exception:
            pass
        return {
            "ok": code == 0,
            "ms": dt,
            "code": code,
            "battery": info,
            "stdout": out[:2000],
            "stderr": err[:2000],
            "hint": None if code == 0 else "Termux:API 没响应：App 装了没？后台被限制了没？",
        }

    def _write(self):
        q = self._query()
        overwrite = q.get("overwrite", ["0"])[0] not in ("0", "", "false", "no")
        scan = q.get("scan", ["1"])[0] not in ("0", "", "false", "no")
        if overwrite and not ALLOW_OVERWRITE:
            raise BridgeError(403, "服务端没开覆盖写（要开：给 phone_server.py 加 --overwrite）")
        abs_path, norm = resolve(q.get("path", [""])[0], need_write=True)
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise BridgeError(411, "缺 Content-Length")
        if n > MAX_WRITE:
            raise BridgeError(413, "超过单次写入上限 %d MB" % (MAX_WRITE // 1048576))
        os.makedirs(os.path.dirname(abs_path) or ROOT, exist_ok=True)

        tmp = abs_path + ".part"
        if overwrite:
            fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
        else:
            try:
                fd = os.open(abs_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                raise BridgeError(409, "手机上已经有同名文件（默认只新建）：%s" % norm)

        got = 0
        try:
            with os.fdopen(fd, "wb") as f:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(262144, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    left -= len(chunk)
            self._body_read = True
            if got != n:
                raise BridgeError(400, "收到的字节数不对：%d/%d" % (got, n))
            if overwrite:
                os.replace(tmp, abs_path)
            try:
                os.chmod(abs_path, 0o644)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp if overwrite else abs_path)
            except OSError:
                pass
            raise
        scanned = media_scan(abs_path) if scan else False
        self._json(200, {"ok": True, "path": norm, "abs": abs_path, "bytes": got, "scanned": scanned})

    def _notify(self, body):
        title = str(body.get("title") or "大肥鱼工作室")
        argv = ["termux-notification", "--title", title]
        if body.get("content"):
            argv += ["--content", str(body["content"])]
        if body.get("id"):
            argv += ["--id", str(body["id"])]
        code, out, err = run_cmd(argv, timeout=15)
        self._json(200 if code == 0 else 500, {"ok": code == 0, "code": code, "stdout": out, "stderr": err})

    def _open(self, body):
        if body.get("target"):
            argv = ["termux-open-url", str(body["target"])]
        elif body.get("path"):
            abs_path, norm = resolve(str(body["path"]), need_write=False)
            if not os.path.exists(abs_path):
                raise BridgeError(404, "手机上没这个文件：%s" % norm)
            argv = ["termux-open", abs_path]
        else:
            raise BridgeError(400, "要么给 target（网址），要么给 path（手机上的文件）")
        code, out, err = run_cmd(argv, timeout=20)
        self._json(200 if code == 0 else 500, {"ok": code == 0, "code": code, "stdout": out, "stderr": err})

    def _photo(self, body):
        d = str(body.get("dir") or "DCIM/大肥鱼")
        abs_dir, norm_dir = resolve(d, need_write=True)
        os.makedirs(abs_dir, exist_ok=True)
        name = "拍照-%s.jpg" % time.strftime("%Y%m%d-%H%M%S")
        abs_file = os.path.join(abs_dir, name)
        cam = str(body.get("camera") or 0)
        code, out, err = run_cmd(["termux-camera-photo", "-c", cam, abs_file], timeout=45)
        ok = code == 0 and os.path.isfile(abs_file)
        if ok:
            media_scan(abs_file)
        self._json(200 if ok else 500, {
            "ok": ok, "code": code, "path": posixpath.join(norm_dir, name) if ok else None,
            "stdout": out, "stderr": err,
        })

    def _scan(self, body):
        abs_path, norm = resolve(str(body.get("path") or ""))
        if not os.path.exists(abs_path):
            raise BridgeError(404, "没有这个路径：%s" % norm)
        done = media_scan(abs_path)
        self._json(200, {"ok": True, "path": norm, "scanned": done,
                         "hint": None if done else "没有 termux-media-scan（pkg install termux-api）"})

    def _pick(self, body):
        """手机上弹系统文件选择器，把用户挑的那个文件收进指定目录。

        termux-storage-get 是「阻塞到用户选完」的，所以这里等的是人，不是网络。
        """
        d = str(body.get("dir") or "Download/大肥鱼/收")
        abs_dir, norm_dir = resolve(d, need_write=True)
        os.makedirs(abs_dir, exist_ok=True)
        name = str(body.get("name") or ("收到-%s" % time.strftime("%Y%m%d-%H%M%S")))
        if "/" in name or name.startswith("."):
            raise BridgeError(400, "文件名不合法")
        abs_file = os.path.join(abs_dir, name)
        if os.path.exists(abs_file):
            raise BridgeError(409, "手机上已经有同名文件：%s" % posixpath.join(norm_dir, name))
        code, out, err = run_cmd(["termux-storage-get", abs_file], timeout=300)
        size = os.path.getsize(abs_file) if os.path.exists(abs_file) else 0
        if code != 0 or size == 0:
            # 取消选择时会留下一个 0 字节文件：那是我们自己刚建的空壳，清掉不留垃圾。
            # （用户的原始文件在手机别处，从来不碰。）
            if size == 0 and os.path.exists(abs_file):
                try:
                    os.unlink(abs_file)
                except OSError:
                    pass
            return self._json(200, {"ok": True, "empty": True, "remote": None,
                                    "code": code, "stderr": err[:2000]})
        media_scan(abs_file)
        self._json(200, {"ok": True, "empty": False, "bytes": size,
                         "remote": posixpath.join(norm_dir, name)})

    def _run(self, body):
        argv = body.get("argv")
        code, out, err = run_cmd(argv, timeout=body.get("timeout") or 30, stdin=body.get("input"))
        self._json(200, {"ok": code == 0, "code": code, "stdout": out, "stderr": err})


def main():
    global ROOT, WRITE_ROOTS, ALLOW_RISKY, ALLOW_OVERWRITE, MAX_WRITE
    ap = argparse.ArgumentParser(description="手机桥服务（只读文件服务 + 白名单执行层）")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--root", default="/storage/emulated/0")
    ap.add_argument("--write-roots", default=",".join(DEFAULT_WRITE_ROOTS),
                    help="允许写入的顶层目录，逗号分隔")
    ap.add_argument("--allow-risky", action="store_true", help="放开短信/通讯录/拨号等危险命令")
    ap.add_argument("--overwrite", action="store_true", help="允许覆盖同名文件（默认只新建）")
    ap.add_argument("--max-write-mb", type=int, default=512)
    a = ap.parse_args()

    ROOT = os.path.abspath(a.root)
    WRITE_ROOTS = [x.strip() for x in a.write_roots.split(",") if x.strip()]
    ALLOW_RISKY = a.allow_risky
    ALLOW_OVERWRITE = a.overwrite
    MAX_WRITE = max(1, a.max_write_mb) * 1024 * 1024
    if not os.path.isdir(ROOT):
        sys.exit("✗ 根目录不存在：%s" % ROOT)

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    print()
    print("  手机桥 · 启动（v%s）" % VERSION)
    print("  ├─ 服务目录：%s" % ROOT)
    print("  ├─ 监听地址：127.0.0.1:%d   （只对本机开放）" % a.port)
    print("  ├─ 可写入：%s%s" % (", ".join(WRITE_ROOTS), "（允许覆盖）" if ALLOW_OVERWRITE else "（只新建）"))
    print("  ├─ 危险命令：%s" % ("已放开" if ALLOW_RISKY else "关闭（短信/通讯录/拨号）"))
    print("  └─ Termux:API：%s" % ("可用" if shutil.which("termux-battery-status") else "未装（只能读文件）"))
    print()
    print("  别关 Termux；按 home 键切走就行。要停就按 音量下 + C。")
    print()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  收到 Ctrl-C，退出。")


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    main()
