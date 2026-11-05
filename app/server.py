#!/usr/bin/env python3
"""
Ebook Prep web UI: a small server around prep_text.py.

Books live in BOOKS_DIR (originals) and cleaned text goes to TEXT_DIR.
Standard library only, so the image needs nothing from pip to run it.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import prep_text

BOOKS = Path(os.environ.get("BOOKS_DIR", "/data/books"))
TEXT = Path(os.environ.get("TEXT_DIR", "/data/text"))
STATIC = Path(__file__).with_name("static")
PORT = int(os.environ.get("PORT", "7880"))
BOOK_TYPES = {".epub", ".pdf", ".txt"}
# Files the page itself loads, besides index.html.
ASSETS = {
    "/icon.svg": "image/svg+xml",
    "/icon-192.png": "image/png",
    "/icon-512.png": "image/png",
    "/apple-touch-icon.png": "image/png",
    "/favicon.ico": "image/png",              # browsers ask for this by default
    "/manifest.webmanifest": "application/manifest+json",
}
JOBS = {}
JOBS_LOCK = threading.Lock()


def safe_name(name, folder, types):
    """A plain file name inside folder with an allowed extension, or None."""
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith(".") or "\x00" in name:
        return None
    if Path(name).suffix.lower() not in types:
        return None
    path = (folder / name).resolve()
    return path if path.parent == folder.resolve() else None


def checks(text):
    return {"check": prep_text.words_to_check(text), "joins": prep_text.split_words(text)}


def output_name(book_name):
    return Path(book_name).stem + ".tts.txt"


def list_files():
    books, texts = [], []
    if BOOKS.is_dir():
        for p in sorted(BOOKS.iterdir(), key=lambda p: p.name.lower()):
            if p.is_file() and p.suffix.lower() in BOOK_TYPES and not p.name.startswith("."):
                st = p.stat()
                books.append({
                    "name": p.name, "size": st.st_size, "mtime": st.st_mtime,
                    "output": output_name(p.name) if (TEXT / output_name(p.name)).exists() else None,
                })
    if TEXT.is_dir():
        for p in sorted(TEXT.iterdir(), key=lambda p: -p.stat().st_mtime):
            if p.is_file() and p.suffix.lower() == ".txt" and not p.name.startswith("."):
                st = p.stat()
                texts.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    return {"books": books, "texts": texts, "books_dir": os.environ.get("BOOKS_HOST", str(BOOKS)),
            "text_dir": os.environ.get("TEXT_HOST", str(TEXT)), "ocr": bool(shutil.which("ocrmypdf")),
            "dictionary": bool(prep_text.dictionary())}


def has_text_layer(pdf):
    try:
        out = subprocess.run(["pdftotext", "-l", "12", "-enc", "UTF-8", str(pdf), "-"],
                             capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return len("".join(out.split())) >= 300


def run_ocr(src, redo, job):
    if not shutil.which("ocrmypdf"):
        raise prep_text.PrepError("OCR isn't available: ocrmypdf is not installed in this image.")
    tmp = Path(tempfile.mkdtemp(prefix="ocr-"))
    out = tmp / "ocr.pdf"
    cmd = ["ocrmypdf", "--force-ocr" if redo else "--skip-text", "-l", "eng",
           "--output-type", "pdf", "--optimize", "0", "--jobs", str(os.cpu_count() or 4),
           str(src), str(out)]
    job_log(job, "Running OCR on every page. This can take several minutes for a long book."
            if redo else "Running OCR on pages that have no text.")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, bufsize=1)
    job["proc"] = proc
    for line in proc.stderr:              # universal newlines also splits progress-bar \r updates
        line = line.strip()
        if line:
            job_log(job, line, replace_progress=True)
    code = proc.wait()
    job.pop("proc", None)
    if job.get("cancelled"):
        raise prep_text.PrepError("Stopped.")
    if code != 0 or not out.exists():
        raise prep_text.PrepError(f"OCR failed (ocrmypdf exit code {code}). The lines above say why.")
    return out, tmp


def job_log(job, line, replace_progress=False):
    log = job["log"]
    # Collapse progress-bar spam: replace the last line if it's the same task updating.
    if replace_progress and log and log[-1].split(":")[0] == line.split(":")[0] and "%" in line:
        log[-1] = line
    else:
        log.append(line)
    del log[:-200]


def run_job(job, book, opts):
    tmp = None
    try:
        src = book
        if book.suffix.lower() == ".pdf" and opts.get("ocr", "auto") != "off":
            redo = opts.get("ocr") == "redo"
            if redo or not has_text_layer(book):
                if not redo:
                    job_log(job, "This PDF has little or no text layer, so it needs OCR.")
                src, tmp = run_ocr(book, redo, job)
        job_log(job, "Cleaning up the text.")
        result = prep_text.process(src, sentences=bool(opts.get("sentences")),
                                   keep_furniture=bool(opts.get("keep_furniture")))
        TEXT.mkdir(parents=True, exist_ok=True)
        out_name = output_name(book.name)
        (TEXT / out_name).write_text(result["text"], encoding="utf-8")
        result["name"] = out_name
        result["book"] = book.name
        job.update(state="done", result=result)
        job_log(job, f"Saved {out_name}.")
    except prep_text.PrepError as e:
        job.update(state="error", error=str(e))
    except Exception as e:                               # report anything unexpected to the page
        job.update(state="error", error=f"{type(e).__name__}: {e}")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        job["finished"] = time.time()


class Handler(BaseHTTPRequestHandler):
    server_version = "EbookPrep/1.0"

    def log_message(self, fmt, *args):
        pass                                             # keep `docker logs` quiet

    # -- helpers
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, msg, code=400):
        self.send_json({"error": msg}, code)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024 * 1024:
            raise ValueError("Request too large")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise ValueError("The request wasn't valid JSON.")

    # -- routes
    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            body = (STATIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path in ASSETS:
            name = "icon-192.png" if url.path == "/favicon.ico" else url.path.lstrip("/")
            body = (STATIC / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ASSETS[url.path])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/files":
            self.send_json(list_files())
        elif url.path.startswith("/api/job/"):
            job = JOBS.get(url.path.rsplit("/", 1)[-1])
            if not job:
                return self.fail("That conversion isn't running anymore.", 404)
            self.send_json({k: v for k, v in job.items() if k != "proc"})
        elif url.path == "/api/text":
            path = safe_name(q.get("name", [""])[0], TEXT, {".txt"})
            if not path or not path.exists():
                return self.fail("Text file not found.", 404)
            text = path.read_text(encoding="utf-8", errors="replace")
            self.send_json({"name": path.name, "text": text, **checks(text)})
        else:
            self.fail("Not found.", 404)

    def do_PUT(self):
        url = urlparse(self.path)
        if url.path != "/api/upload":
            return self.fail("Not found.", 404)
        name = parse_qs(url.query).get("name", [""])[0]
        path = safe_name(name, BOOKS, BOOK_TYPES)
        if not path:
            return self.fail("Upload an .epub, .pdf or .txt file.")
        remaining = int(self.headers.get("Content-Length") or 0)
        BOOKS.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("." + path.name + ".part")
        with open(tmp, "wb") as fh:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        if remaining:
            tmp.unlink(missing_ok=True)
            return self.fail("The upload was cut off. Try again.")
        tmp.replace(path)
        self.send_json({"name": path.name})

    def do_POST(self):
        url = urlparse(self.path)
        try:
            data = self.read_json()
        except ValueError as e:
            return self.fail(str(e))
        if url.path == "/api/convert":
            book = safe_name(data.get("name"), BOOKS, BOOK_TYPES)
            if not book or not book.exists():
                return self.fail("Book not found.", 404)
            job = {"id": uuid.uuid4().hex[:12], "state": "running", "book": book.name,
                   "log": [], "started": time.time()}
            with JOBS_LOCK:
                for old in [k for k, j in JOBS.items() if j.get("finished", time.time()) < time.time() - 3600]:
                    JOBS.pop(old, None)
                JOBS[job["id"]] = job
            threading.Thread(target=run_job, args=(job, book, data), daemon=True).start()
            self.send_json({"id": job["id"]})
        elif url.path.startswith("/api/job/") and url.path.endswith("/stop"):
            job = JOBS.get(url.path.split("/")[3])
            if job and job.get("proc"):
                job["cancelled"] = True
                job["proc"].terminate()
            self.send_json({"ok": True})
        elif url.path == "/api/save":
            path = safe_name(data.get("name"), TEXT, {".txt"})
            if not path:
                return self.fail("Save as a .txt name without folders.")
            TEXT.mkdir(parents=True, exist_ok=True)
            text = data.get("text", "")
            path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
            self.send_json({"name": path.name, **checks(text)})
        elif url.path == "/api/check":
            self.send_json(checks(data.get("text", "")))
        else:
            self.fail("Not found.", 404)

    def do_DELETE(self):
        url = urlparse(self.path)
        name = parse_qs(url.query).get("name", [""])[0]
        if url.path == "/api/book":
            path = safe_name(name, BOOKS, BOOK_TYPES)
            if path and any(j["state"] == "running" and j["book"] == path.name for j in JOBS.values()):
                return self.fail("That book is being converted. Wait for it to finish.", 409)
        elif url.path == "/api/text":
            path = safe_name(name, TEXT, {".txt"})
        else:
            return self.fail("Not found.", 404)
        if not path or not path.exists():
            return self.fail("File not found.", 404)
        path.unlink()
        self.send_json({"ok": True})


def main():
    BOOKS.mkdir(parents=True, exist_ok=True)
    TEXT.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=prep_text.dictionary, daemon=True).start()   # warm the word list
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Ebook Prep running on port {PORT}. Books: {BOOKS}  Text: {TEXT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
