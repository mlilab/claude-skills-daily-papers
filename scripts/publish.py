#!/usr/bin/env python3
"""Publish a password-protected copy of the digest to GitHub Pages (https://<user>.github.io/<repo>/).

usage: publish.py set-password            # asks twice on a terminal, or reads one line from stdin
       publish.py build --out DIR         # encrypted copy into DIR
       publish.py push                    # build into the papers repo and force-push (config: publish)

Static hosting cannot check passwords, so every HTML page
is encrypted (AES-256-GCM, key from PBKDF2-SHA256 with 600,000 iterations and a per-site salt)
and decrypted in the browser with WebCrypto. On top of that, because the digest has pictures and
names that reveal what is being read:
- figure images are encrypted too (`.bin` files, decrypted into blob URLs after unlock);
- paper folders get opaque names (keyed HMAC of the arXiv id), so the public file tree does not
  list which papers were read;
- pages are gzip-compressed before encryption to keep the repo small;
- figures are published only for the last `publish.figure_days` days (pages are kept forever).
Only the rendered pages, the figures they show and the CSS/JS go out: candidates, selections,
full texts, PDFs, summaries JSON, affiliations and logs never leave the machine.

The derived key (not the password) lives in <output_dir>/.publish/key.json (mode 600). The repo
keeps exactly one commit, force-pushed each time, so nothing accumulates in public history.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import gzip
import hashlib
import hmac
import io
import json
import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path

from common import load_config, local_tz

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 600_000
MARKER = ".dp-publish"
IMG_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
           ".webp": "image/webp", ".svg": "image/svg+xml"}
BLANK = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731


def derive(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(password.encode("utf-8"))


def key_path(cfg) -> Path:
    return cfg["output_dir"] / ".publish" / "key.json"


def set_password(path: Path):
    if sys.stdin.isatty():
        pw = getpass.getpass("Daily Papers 비밀번호: ")
        if pw != getpass.getpass("한 번 더: "):
            sys.exit("비밀번호가 서로 다릅니다")
    else:
        pw = sys.stdin.readline().rstrip("\n")
    if len(pw) < 12:
        sys.exit("비밀번호는 12자 이상으로 해주세요 (오프라인 대입 공격 대비)")
    salt = os.urandom(16)
    data = {"v": 1, "salt": b64(salt), "iter": ITERATIONS, "key": b64(derive(pw, salt))}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)
    print(json.dumps({"key_file": str(path), "note": "password set: run `publish.py push`"},
                     ensure_ascii=False))


LOCK_PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive">
<title>Daily Papers</title>
<style>
:root{--bg:#f7f6f2;--card:#fff;--text:#1d1d1f;--muted:#6b6b70;--line:#e2dfd7;--accent:#2f5bd3;--err:#a8341f;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#131416;--card:#1c1d20;--text:#ececee;--muted:#9a9aa2;--line:#2e3035;--accent:#8aa8ff;--err:#ff8f7a;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",system-ui,sans-serif;padding:16px}
.lock{width:100%;max-width:360px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px 24px}
h1{font-size:20px;margin:0 0 4px}p{margin:0 0 16px;color:var(--muted);font-size:14px}
input[type=password]{width:100%;font:inherit;font-size:16px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text)}
label{display:flex;gap:6px;align-items:center;font-size:13.5px;color:var(--muted);margin:10px 0 14px}
button{width:100%;font:inherit;font-size:15px;font-weight:600;padding:10px;border:0;border-radius:10px;background:var(--accent);color:#fff;cursor:pointer}
button:disabled{opacity:.6;cursor:wait}#dp-err{color:var(--err);font-size:13.5px;margin:12px 0 0}
</style>
</head>
<body>
<main class="lock" id="dp-lock" hidden>
<h1>Daily Papers</h1>
<p>비밀번호를 입력해야 볼 수 있습니다.</p>
<form id="dp-form">
<input type="password" id="dp-pw" autocomplete="current-password" aria-label="비밀번호" required autofocus>
<label><input type="checkbox" id="dp-remember"> 이 기기에서 기억하기</label>
<button id="dp-go" type="submit">열기</button>
<p id="dp-err" role="alert" hidden>비밀번호가 맞지 않습니다.</p>
</form>
</main>
<noscript>이 페이지는 JavaScript로 복호화됩니다.</noscript>
<script id="dp-payload" type="application/json">__PAYLOAD__</script>
<script>
(async function () {
  var P = JSON.parse(document.getElementById('dp-payload').textContent);
  var K = 'rn-key-' + P.salt;
  function bytes(s) { return Uint8Array.from(atob(s), function (c) { return c.charCodeAt(0); }); }
  function text(b) { var s = ''; b.forEach(function (x) { s += String.fromCharCode(x); }); return btoa(s); }
  function get() { try { return sessionStorage.getItem(K) || localStorage.getItem(K); } catch (e) { return null; } }
  function forget() { try { sessionStorage.removeItem(K); localStorage.removeItem(K); } catch (e) {} }
  async function decrypt(raw) {
    var key = await crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['decrypt']);
    var pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: bytes(P.iv) }, key, bytes(P.ct));
    if (!P.z) return new TextDecoder().decode(pt);
    var stream = new Blob([pt]).stream().pipeThrough(new DecompressionStream('gzip'));
    return await new Response(stream).text();
  }
  function show(html) {
    // swap the DOM in place (document.write during page load can leave the load hanging)
    var doc = new DOMParser().parseFromString(html, 'text/html');
    document.replaceChild(document.importNode(doc.documentElement, true), document.documentElement);
    document.querySelectorAll('script').forEach(function (old) {
      var s = document.createElement('script');  // imported scripts don't run; recreate them
      Array.prototype.forEach.call(old.attributes, function (a) { s.setAttribute(a.name, a.value); });
      if (old.src) s.async = false;  // keep document order (KaTeX before its auto-render)
      s.text = old.text;
      old.replaceWith(s);
    });
  }
  var saved = get();
  if (saved) {
    try { show(await decrypt(bytes(saved))); return; } catch (e) { forget(); }
  }
  var lock = document.getElementById('dp-lock');
  lock.hidden = false;
  document.getElementById('dp-form').addEventListener('submit', async function (ev) {
    ev.preventDefault();
    var go = document.getElementById('dp-go'), err = document.getElementById('dp-err');
    go.disabled = true; err.hidden = true;
    try {
      var pw = new TextEncoder().encode(document.getElementById('dp-pw').value);
      var base = await crypto.subtle.importKey('raw', pw, 'PBKDF2', false, ['deriveBits']);
      var raw = new Uint8Array(await crypto.subtle.deriveBits(
        { name: 'PBKDF2', hash: 'SHA-256', salt: bytes(P.salt), iterations: P.iter }, base, 256));
      var html = await decrypt(raw);
      try {
        sessionStorage.setItem(K, text(raw));
        if (document.getElementById('dp-remember').checked) localStorage.setItem(K, text(raw));
      } catch (e) {}
      show(html);
    } catch (e) {
      err.hidden = false; go.disabled = false;
      document.getElementById('dp-pw').select();
    }
  });
})();
</script>
</body>
</html>
"""

# added to every decrypted page: decrypts the figure images with the key the lock page stored
IMG_LOADER = """<script>
(function () {
  var K = 'rn-key-__SALT__', raw = null;
  try { raw = sessionStorage.getItem(K) || localStorage.getItem(K); } catch (e) {}
  if (!raw) return;
  var keyP = crypto.subtle.importKey('raw', Uint8Array.from(atob(raw), function (c) { return c.charCodeAt(0); }),
                                     'AES-GCM', false, ['decrypt']);
  var cache = {};
  function load(url, mime) {
    return cache[url] || (cache[url] = fetch(url).then(function (r) { return r.arrayBuffer(); })
      .then(function (buf) {
        var b = new Uint8Array(buf);
        return keyP.then(function (k) {
          return crypto.subtle.decrypt({ name: 'AES-GCM', iv: b.slice(0, 12) }, k, b.slice(12));
        });
      }).then(function (pt) {
        if (mime !== 'image/svg+xml') return new Blob([pt], { type: mime });
        var st = new Blob([pt]).stream().pipeThrough(new DecompressionStream('gzip'));
        return new Response(st).arrayBuffer().then(function (b) { return new Blob([b], { type: mime }); });
      }).then(function (blob) { return URL.createObjectURL(blob); }));
  }
  document.querySelectorAll('img[data-enc]').forEach(function (img) {
    load(img.getAttribute('data-enc'), img.getAttribute('data-mime')).then(function (u) { img.src = u; });
  });
  document.querySelectorAll('a[data-enc-href]').forEach(function (a) {
    load(a.getAttribute('data-enc-href'), a.getAttribute('data-mime')).then(function (u) { a.href = u; });
  });
})();
</script>
<style>img[data-off]{display:none}</style>
"""

LOCK_BUTTON = """<button type="button" class="rn-lockbtn" onclick="(function(){try{[sessionStorage,localStorage].forEach(function(s){Object.keys(s).forEach(function(k){if(k.indexOf('rn-key-')===0)s.removeItem(k);});});}catch(e){}location.reload();})()">잠그기</button>
<style>.rn-lockbtn{position:fixed;right:14px;bottom:14px;z-index:60;font:inherit;font-size:13px;padding:6px 12px;border-radius:999px;border:1px solid var(--line,#ddd);background:var(--surface,#fff);color:var(--muted,#666);cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.08)}</style>
"""

ATTR = re.compile(r'\b(src|href)="([^"]*)"')
IMG_TAG = re.compile(r"<img\b[^>]*>")


class Site:
    """Maps every published source file to its path in the output tree."""

    def __init__(self, cfg, key: bytes):
        self.cfg, self.key = cfg, key
        self.src = cfg["output_dir"].resolve()
        self.map: dict[Path, str] = {}       # source file -> output relpath
        self.images: dict[Path, str] = {}    # source image -> output relpath (.bin)
        today = dt.datetime.now(local_tz(cfg)).date()
        days = (cfg.get("publish") or {}).get("figure_days", 30)
        self.fig_cutoff = today - dt.timedelta(days=int(days)) if days is not None else None

    def opaque(self, *parts) -> str:
        return hmac.new(self.key, "\0".join(parts).encode(), hashlib.sha256).hexdigest()[:12]

    def issues(self):
        return sorted(p for p in self.src.glob("????-??-??") if (p / "en.html").exists())

    def collect(self):
        if (self.src / "index.html").exists():
            self.map[self.src / "index.html"] = "index.html"
        for f in sorted((self.src / "assets").glob("*")):
            if f.suffix in (".css", ".js"):
                self.map[f] = f"assets/{f.name}"
        for issue in self.issues():
            d = issue.name
            for name in ("en.html", "ko.html", "index.html"):
                if (issue / name).exists():
                    self.map[issue / name] = f"{d}/{name}"
            for pdir in sorted((issue / "papers").glob("*")):
                if not (pdir / "en.html").exists():
                    continue
                h = self.opaque("paper", d, pdir.name)
                for name in ("en.html", "ko.html", "index.html"):
                    if (pdir / name).exists():
                        self.map[pdir / name] = f"{d}/p/{h}/{name}"

    def figures_allowed(self, path: Path) -> bool:
        try:
            d = dt.date.fromisoformat(path.relative_to(self.src).parts[0])
        except (ValueError, IndexError):
            return False
        return self.fig_cutoff is None or d >= self.fig_cutoff

    def image_out(self, img: Path) -> str | None:
        """Output path for an image referenced by a page, or None if it is not published."""
        if img in self.images:
            return self.images[img]
        try:
            rel = img.relative_to(self.src).parts
        except ValueError:
            return None
        if len(rel) < 4 or rel[1] != "papers" or not img.is_file() or not self.figures_allowed(img):
            return None
        d, pid = rel[0], rel[2]
        h = self.opaque("paper", d, pid)
        out = f"{d}/p/{h}/f/{self.opaque('fig', d, pid, *rel[3:])}.bin"
        self.images[img] = out
        return out

    def rewrite(self, page_src: Path, html: str) -> str:
        out_dir = posixpath.dirname(self.map[page_src])
        base = page_src.parent

        def resolve(url):
            if not url or url.startswith(("#", "http:", "https:", "data:", "mailto:", "javascript:")):
                return None
            path = url.split("#")[0].split("?")[0]
            return Path(os.path.normpath(base / path))

        def rel_to(target_rel):
            return posixpath.relpath(target_rel, out_dir or ".")

        def fix_img(m):
            tag = m.group(0)
            sm = re.search(r'\bsrc="([^"]*)"', tag)
            if not sm:
                return tag
            p = resolve(sm.group(1))
            if p is None or p.suffix.lower() not in IMG_EXT:
                return tag
            out = self.image_out(p)
            if out is None:
                return tag.replace(sm.group(0), f'src="{BLANK}" data-off="1"')
            mime = "image/svg+xml" if p.suffix.lower() == ".svg" else "image/webp"
            return tag.replace(sm.group(0), f'src="{BLANK}" data-enc="{rel_to(out)}" data-mime="{mime}"')

        html = IMG_TAG.sub(fix_img, html)

        def fix_attr(m):
            attr, url = m.group(1), m.group(2)
            p = resolve(url)
            if p is None:
                return m.group(0)
            if p.suffix.lower() in IMG_EXT and attr == "href":
                out = self.image_out(p)
                if out is None:
                    return 'href="#"'
                mime = "image/svg+xml" if p.suffix.lower() == ".svg" else "image/webp"
                return f'href="#" data-enc-href="{rel_to(out)}" data-mime="{mime}"'
            target = p / "index.html" if url.endswith("/") else p
            if target in self.map:
                frag = "#" + url.split("#", 1)[1] if "#" in url else ""
                return f'{attr}="{rel_to(self.map[target])}{frag}"'
            return m.group(0)

        return ATTR.sub(fix_attr, html)


def leak_patterns(cfg) -> list[tuple[str, re.Pattern]]:
    """What must never appear in anything published: tokens (by shape and the actual stored values)
    and facts about this machine (hostname, account home path, local server addresses)."""
    import getpass
    import socket
    pats = [("GitHub token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")),
            ("Slack bot token", re.compile(r"\bxoxb-[A-Za-z0-9-]{20,}")),
            ("local server address", re.compile(r"\b(localhost|127\.0\.0\.1|0\.0\.0\.0):\d+")),
            ("home directory path", re.compile(re.escape(str(Path.home())) + r"\b"))]
    host = socket.gethostname()
    if len(host) >= 4:
        pats.append(("server hostname", re.compile(re.escape(host), re.I)))
    user = getpass.getuser()
    if len(user) >= 4:
        pats.append(("server account path", re.compile(r"/(home|Users)/" + re.escape(user) + r"\b")))
    for tf in {(cfg.get("publish") or {}).get("token_file"),
               (cfg.get("feedback") or {}).get("token_file"),
               cfg["output_dir"] / ".publish" / "slack-token"}:
        if tf and Path(os.path.expanduser(tf)).exists():
            tok = Path(os.path.expanduser(tf)).read_text().strip()
            if len(tok) >= 20:
                pats.append(("stored token value", re.compile(re.escape(tok))))
    member_file = cfg["output_dir"] / ".publish" / "slack-member-id"
    if member_file.exists():
        member = member_file.read_text().strip()
        if member:
            pats.append(("Slack recipient ID", re.compile(re.escape(member))))
    return pats


def check_leaks(pats, rel: str, text: str):
    for name, pat in pats:
        if pat.search(text):
            raise SystemExit(f"publish refused: {name} found in {rel} (nothing was pushed)")


def image_bytes(path: Path) -> bytes:
    """Web-sized payload: SVG gzipped (the loader inflates it), rasters as WebP (max 1400 px)."""
    if path.suffix.lower() == ".svg":
        return gzip.compress(path.read_bytes(), compresslevel=9, mtime=0)
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGBA") if im.mode in ("P", "LA", "RGBA") else im.convert("RGB")
        if im.width > 1400:
            im = im.resize((1400, round(im.height * 1400 / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=82, method=6)
        return buf.getvalue()


def encrypt_page(html: str, key: bytes, salt_b64: str, iterations: int) -> str:
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, gzip.compress(html.encode("utf-8"), mtime=0), None)
    payload = json.dumps({"v": 1, "z": 1, "salt": salt_b64, "iter": iterations,
                          "iv": b64(iv), "ct": b64(ct)})
    return LOCK_PAGE.replace("__PAYLOAD__", payload)


def build(cfg, out: Path, kp: Path) -> dict:
    if not kp.exists():
        sys.exit(f"no key yet: run `publish.py set-password` first ({kp})")
    k = json.loads(kp.read_text())
    key = base64.b64decode(k["key"])
    out = out.resolve()
    src = cfg["output_dir"].resolve()
    if out == src or (src in out.parents and not any(
            x.startswith(".") for x in out.relative_to(src).parts)):
        sys.exit("--out must be outside the digest folder (or a hidden folder inside it)")
    visible = [x for x in out.iterdir() if not x.name.startswith(".")] if out.exists() else []
    if visible and not (out / MARKER).exists():
        sys.exit(f"{out} is not empty and was not made by publish.py; refusing to overwrite")
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("managed by daily-papers publish.py\n")
    (out / ".nojekyll").write_text("")

    site = Site(cfg, key)
    site.collect()
    pats = leak_patterns(cfg)
    key_id = hashlib.sha256(key).hexdigest()[:16]
    # unchanged sources keep their previous ciphertext (a fresh IV would rewrite every file in git)
    state_file = kp.parent / "publish-state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    prev = state.get(str(out), {})
    seen, written, rewritten = {}, {MARKER}, 0
    loader = IMG_LOADER.replace("__SALT__", k["salt"])

    def emit(rel: str, digest: str, make):
        nonlocal rewritten
        seen[rel] = digest
        written.add(rel)
        dst = out / rel
        if prev.get(rel) == digest and dst.exists():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = make()
        (dst.write_bytes if isinstance(data, bytes) else dst.write_text)(data)
        rewritten += 1

    for page, rel in site.map.items():
        if page.suffix != ".html":
            check_leaks(pats, rel, page.read_text(encoding="utf-8", errors="replace"))
            emit(rel, hashlib.sha256(page.read_bytes()).hexdigest(), lambda p=page: p.read_bytes())
            continue
        html = page.read_text(encoding="utf-8")
        check_leaks(pats, rel, html)  # also inside pages that get encrypted
        if page.name == "index.html" and page != src / "index.html":
            emit(rel, hashlib.sha256(html.encode()).hexdigest(), lambda h=html: h)  # language redirect
            continue
        html = site.rewrite(page, html)
        html = html.replace("<head>", '<head>\n<meta name="robots" content="noindex, nofollow, noarchive">', 1)
        html = html.replace("</body>", loader + LOCK_BUTTON + "</body>", 1)
        digest = hashlib.sha256((key_id + LOCK_PAGE + html).encode("utf-8")).hexdigest()
        emit(rel, digest, lambda h=html: encrypt_page(h, key, k["salt"], k["iter"]))

    aes = AESGCM(key)
    for img, rel in site.images.items():
        digest = hashlib.sha256(key_id.encode() + img.read_bytes()).hexdigest()

        def make(p=img):
            iv = os.urandom(12)
            return iv + aes.encrypt(iv, image_bytes(p), None)
        emit(rel, digest, make)

    removed = []
    for f in sorted(out.rglob("*")):
        parts = f.relative_to(out).parts
        if any(x.startswith(".") for x in parts):
            continue
        if f.is_file() and f.relative_to(out).as_posix() not in written:
            f.unlink()
            removed.append(f.relative_to(out).as_posix())
    for d in sorted((d for d in out.rglob("*") if d.is_dir()), reverse=True):
        if not any(x.startswith(".") for x in d.relative_to(out).parts) and not any(d.iterdir()):
            d.rmdir()
    state[str(out)] = seen
    state_file.write_text(json.dumps(state, indent=1))
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file() and ".git" not in f.parts)
    return {"out": str(out), "files": len(seen), "pages": sum(1 for r in seen if r.endswith(".html")),
            "images": len(site.images), "rewritten": rewritten, "removed": len(removed),
            "megabytes": round(size / 1e6, 1)}


# ---------------------------------------------------------------- push (single force-pushed commit)


def git(repo: Path, *args, check=True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def ensure_repo(pc: dict) -> Path:
    repo = Path(os.path.expanduser(pc["repo_dir"]))
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        git(repo, "init", "-q", "-b", pc["branch"])
        git(repo, "remote", "add", "origin", pc["remote"])
    git(repo, "config", "user.name", pc["github_user"])
    git(repo, "config", "user.email", pc["git_email"])
    git(repo, "config", "--unset-all", "credential.helper", check=False)
    git(repo, "config", "--add", "credential.helper", "")  # drop global helpers for this repo
    token_file = os.path.expanduser(pc["token_file"])
    git(repo, "config", "--add", "credential.helper",
        f'!f() {{ test "$1" = get && echo username={pc["github_user"]} && echo "password=$(cat {token_file})"; }}; f')
    return repo


def push(cfg, kp: Path) -> dict:
    pc = cfg.get("publish") or {}
    if not pc.get("enabled"):
        return {"skipped": "publish.enabled is false"}
    repo = ensure_repo(pc)
    summary = build(cfg, repo, kp)
    git(repo, "add", "-A")
    tree = git(repo, "write-tree")
    head_tree = git(repo, "rev-parse", "HEAD^{tree}", check=False)
    remote = git(repo, "ls-remote", "origin", f"refs/heads/{pc['branch']}", check=False).split()
    head = git(repo, "rev-parse", "HEAD", check=False)
    if tree == head_tree and remote and remote[0] == head:
        return {**summary, "pushed": False, "reason": "no change", "url": pc.get("url")}
    commit = git(repo, "commit-tree", tree, "-m", "update")
    git(repo, "update-ref", f"refs/heads/{pc['branch']}", commit)
    git(repo, "push", "--force", "-q", "origin", f"{pc['branch']}:{pc['branch']}")
    return {**summary, "pushed": True, "commit": commit[:10], "url": pc.get("url")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["set-password", "build", "push"])
    ap.add_argument("--out")
    args = ap.parse_args()
    cfg = load_config()
    kp = key_path(cfg)
    if args.cmd == "set-password":
        set_password(kp)
    elif args.cmd == "push":
        print(json.dumps(push(cfg, kp), ensure_ascii=False, indent=2))
    else:
        if not args.out:
            sys.exit("build needs --out DIR")
        print(json.dumps(build(cfg, Path(os.path.expanduser(args.out)), kp), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
