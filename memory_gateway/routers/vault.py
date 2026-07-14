"""Obsidian vault 浏览器（只读）。
挂载在 Memory Gateway 上，提供 tree / file / backlinks / search 4 个接口。
- vault 真源走 OBSIDIAN_VAULT_PATH env var；默认 /home/kidemon/obsidian-vault
- 服务端渲染 Markdown（fenced_code / tables / toc / codehilite）+ wikilinks 解析
- 双链 → <a href="/vault/page?path=目标.md">显示名</a>
- 路径越权硬防：resolve 后必须 startswith(VAULT)
- 单文件 ≤ 1MB
- API key 鉴权复用 api_key_middleware；UI 路径走浏览器登录（admin login 签 session）
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import markdown as md_lib
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

router = APIRouter(prefix="/vault", tags=["vault"])

# ── vault 路径解析 ────────────────────────────────────────
# 优先级：env OBSIDIAN_VAULT_PATH > 软链 > 默认
VAULT = Path(
    os.environ.get("OBSIDIAN_VAULT_PATH")
    or "/home/kidemon/obsidian-vault"
).resolve()

MAX_FILE_BYTES = 1024 * 1024          # 1 MB
MD_EXT = {".md", ".markdown"}

# ── 缓存：避免每请求扫一遍目录树 ──────────────────────────
_TREE_CACHE: dict[str, tuple[float, list]] = {}      # {root_key: (mtime, tree)}
_TREE_TTL = 30.0                                     # 秒
_TREE_BACKLINK_CACHE: dict[str, tuple[float, list]] = {}
_TREE_BACKLINK_TTL = 60.0

# ── Markdown 实例（单例）─────────────────────────────────
_md = md_lib.Markdown(
    extensions=[
        "fenced_code",
        "tables",
        "toc",
        "codehilite",
        "attr_list",
    ],
    extension_configs={
        "codehilite": {"css_class": "codehilite", "guess_lang": False},
    },
)


def _safe_resolve(rel: str) -> Optional[Path]:
    """防 ../ 越权；rel 可以是文件或目录的 vault 相对路径。"""
    # 空路径 → vault 根
    rel = (rel or "").strip().lstrip("/")
    p = (VAULT / rel).resolve() if rel else VAULT
    # resolve 后必须仍在 VAULT 下
    try:
        p.relative_to(VAULT)
    except ValueError:
        return None
    return p


# ── Wikilinks post-processor ─────────────────────────────
# 匹配 [[目标]] 或 [[目标#锚点]] 或 [[目标|显示]]
# 目标可能是文件名（无后缀）或 文件.md 或 子目录/文件名
WIKILINK_RE = re.compile(
    r"""
    \[\[                          # 开头 [[
    ([^\]\|#]+?)                  # 目标（不含 ]|#|，非贪婪）
    (?:\#([^\]\|]+?))?            # 可选 #锚点
    (?:\|([^\]]+?))?              # 可选 |别名
    \]\]                          # 结尾 ]]
    """,
    re.VERBOSE,
)


def _resolve_wikilink(target: str, current_path: str) -> tuple[str | None, str | None]:
    """把 [[target]] 解析成 (相对 URL 或 None, 锚点 或 None)。

    解析顺序：
      1. target 作为文件（含 .md 自动补、README.md 优先）
      2. target 作为目录 → 返回 /vault/?dir=target 目录浏览页
      3. 都找不到 → None
    """
    target = target.strip().rstrip("/")   # 去掉尾部 / 便于处理目录链接
    anchor: str | None = None
    if "#" in target:
        target_file, anchor = target.split("#", 1)
        target = target_file

    # 候选文件路径
    file_candidates: list[Path] = []
    if any(target.endswith(ext) for ext in MD_EXT):
        file_candidates.append(VAULT / target)
    else:
        file_candidates.append(VAULT / (target + ".md"))
        file_candidates.append(VAULT / target / "README.md")
        file_candidates.append(VAULT / target / "index.md")
    # 当前目录优先
    if "/" not in target and current_path:
        cur_dir = (VAULT / current_path).parent
        file_candidates.insert(0, cur_dir / (target + ".md"))

    for c in file_candidates:
        try:
            c = c.resolve()
            c.relative_to(VAULT)
            if c.is_file():
                rel = c.relative_to(VAULT).as_posix()
                # 返回特殊前缀以便 caller 区分：实际处理移到 _render_wikilinks
                return (rel, anchor)
        except (ValueError, OSError):
            continue

    # 文件找不到 → 尝试作为目录
    dir_candidate = (VAULT / target).resolve() if target else VAULT
    try:
        dir_candidate.relative_to(VAULT)
        if dir_candidate.is_dir():
            # 用 dir: 前缀标识是目录链接
            return (f"dir:{dir_candidate.relative_to(VAULT).as_posix()}", anchor)
    except (ValueError, OSError):
        pass

    return (None, anchor)


def _render_wikilinks(html: str, current_path: str) -> str:
    """把已渲染 HTML 里的 [[...]] 残留片段替换成 <a>。"""
    def repl(m: re.Match) -> str:
        target, anchor, alias = m.group(1), m.group(2), m.group(3)
        rel, _ = _resolve_wikilink(target, current_path)
        display = (alias or target).strip()
        if rel is None:
            return f'<span class="wikilink-broken" title="未找到: {target}">{display}</span>'
        if rel.startswith("dir:"):
            dir_name = rel[4:]
            href = f"/vault/?dir={dir_name}"
            if anchor:
                href += "#" + anchor
            return f'<a class="wikilink" href="{href}">{display}</a>'
        href = f"/vault/page?path={rel}"
        if anchor:
            href += "#" + anchor
        return f'<a class="wikilink" href="{href}">{display}</a>'

    return WIKILINK_RE.sub(repl, html)


# ── Tree ──────────────────────────────────────────────────

def _build_tree(root: Path, base: Path) -> list[dict]:
    items: list[dict] = []
    try:
        for entry in sorted(root.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            # 隐藏文件/目录跳过（.git、.obsidian、点开头）
            if entry.name.startswith("."):
                continue
            rel = entry.relative_to(base).as_posix()
            if entry.is_dir():
                items.append({
                    "type": "dir",
                    "name": entry.name,
                    "path": rel,
                    "children": _build_tree(entry, base),
                })
            elif entry.is_file() and entry.suffix.lower() in MD_EXT:
                size = entry.stat().st_size
                items.append({
                    "type": "file",
                    "name": entry.name,
                    "path": rel,
                    "size": size,
                })
    except PermissionError:
        pass
    return items


def _get_tree() -> list[dict]:
    """带缓存的目录树。"""
    import time
    key = str(VAULT)
    now = time.time()
    cached = _TREE_CACHE.get(key)
    if cached and now - cached[0] < _TREE_TTL:
        return cached[1]
    tree = _build_tree(VAULT, VAULT)
    _TREE_CACHE[key] = (now, tree)
    return tree


# ── Routes ───────────────────────────────────────────────

@router.get("/api/tree")
def api_tree():
    """返回 vault 目录树（JSON）。"""
    return {"vault": str(VAULT), "tree": _get_tree()}


@router.get("/api/file")
def api_file(path: str = Query("")):
    """读取单个 markdown 文件 + 渲染后的 HTML。"""
    p = _safe_resolve(path)
    if not p or not p.exists():
        raise HTTPException(404, f"not found: {path}")
    if p.is_dir():
        raise HTTPException(400, "is a directory; use /api/tree")
    if p.suffix.lower() not in MD_EXT:
        raise HTTPException(400, "not a markdown file")
    size = p.stat().st_size
    if size > MAX_FILE_BYTES:
        raise HTTPException(413, f"file too large: {size} bytes")
    raw = p.read_text(encoding="utf-8", errors="replace")
    _md.reset()
    html = _md.convert(raw)
    html = _render_wikilinks(html, path)
    return {
        "path": path,
        "raw": raw,
        "html": html,
        "size": size,
        "mtime": int(p.stat().st_mtime),
    }


@router.get("/api/search")
def api_search(q: str = Query(..., min_length=1), limit: int = 20):
    """轻量全文搜索（文件名 + 内容 substring）。返回 top N。
    注意：vault 只 24 文件、max 5.8KB，没必要上 FTS5。"""
    q_lower = q.lower()
    hits: list[dict] = []
    for md_file in VAULT.rglob("*.md"):
        if any(part.startswith(".") for part in md_file.parts):
            continue
        rel = md_file.relative_to(VAULT).as_posix()
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        score = 0
        for line in text.splitlines():
            if q in line or q_lower in line.lower():
                score += 1
        if q_lower in rel.lower():
            score += 5
        if score > 0:
            snippet = ""
            for line in text.splitlines():
                if q in line:
                    snippet = line.strip()[:200]
                    break
            hits.append({
                "path": rel,
                "score": score,
                "snippet": snippet,
                "mtime": int(md_file.stat().st_mtime),
            })
    hits.sort(key=lambda x: (-x["score"], -x["mtime"]))
    return {"q": q, "hits": hits[:limit], "total": len(hits)}


@router.get("/api/backlinks")
def api_backlinks(path: str = Query(...)):
    """反向链接：哪些文件 [[本页]] 或包含本页文件名。"""
    import time
    now = time.time()
    cached = _TREE_BACKLINK_CACHE.get(path)
    if cached and now - cached[0] < _TREE_BACKLINK_TTL:
        return {"path": path, "backlinks": cached[1]}

    target_name = Path(path).stem                          # 智联业务预案
    pattern = re.compile(rf"\[\[([^\]]*?{re.escape(target_name)}[^\]]*?)\]\]")
    backlinks: list[dict] = []
    for md_file in VAULT.rglob("*.md"):
        if md_file.match(path):
            continue
        if any(part.startswith(".") for part in md_file.parts):
            continue
        rel = md_file.relative_to(VAULT).as_posix()
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in pattern.finditer(text):
            ref = m.group(1).split("|")[0].split("#")[0].strip()
            # 找该引用所在行
            line_no = text[:m.start()].count("\n") + 1
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            if line_end < 0:
                line_end = len(text)
            snippet = text[line_start:line_end].strip()[:200]
            backlinks.append({
                "from": rel,
                "ref": ref,
                "line": line_no,
                "snippet": snippet,
            })
    backlinks.sort(key=lambda x: x["from"])
    _TREE_BACKLINK_CACHE[path] = (now, backlinks)
    return {"path": path, "backlinks": backlinks}


# ── 浏览器 UI ─────────────────────────────────────────────

@router.get("/page", response_class=HTMLResponse)
def page(path: str = Query("")):
    """渲染单个 markdown 为完整 HTML（服务端）。"""
    # vault 缺失时友好提示
    if not VAULT.exists() or not VAULT.is_dir():
        return HTMLResponse(
            f"""<!doctype html><html><head><meta charset=utf-8><title>vault · 配置缺失</title>
            <link rel="stylesheet" href="/static/vault.css"></head>
            <body><div style="max-width:560px;margin:80px auto">
            <h1>📚 vault 路径未配置</h1>
            <p>当前 OBSIDIAN_VAULT_PATH = <code>{VAULT}</code></p>
            <p>请配置真源后重启容器。</p>
            </div></body></html>""",
            status_code=503,
        )
    p = _safe_resolve(path)
    if not p or not p.exists():
        # 不存在 → 返回提示页（200，不报错）
        return HTMLResponse(
            f"""<!doctype html><html><head><meta charset=utf-8><title>404 · vault</title>
            <link rel="stylesheet" href="/static/vault.css"></head>
            <body><div class="wrap"><h1>未找到</h1>
            <p>路径：<code>{path}</code> 不存在或不在 vault 内。</p>
            <p><a href="/vault">← 返回 vault 首页</a></p></div></body></html>""",
            status_code=404,
        )
    if p.is_dir():
        return RedirectResponse(url=f"/vault/?dir={path}", status_code=302)
    size = p.stat().st_size
    if size > MAX_FILE_BYTES:
        return HTMLResponse(f"<h1>文件过大：{size} bytes</h1>", status_code=413)
    raw = p.read_text(encoding="utf-8", errors="replace")
    _md.reset()
    html = _md.convert(raw)
    html = _render_wikilinks(html, path)

    # 反向链接（独立接口，先异步拉；此处只渲染内容）
    # title 取第一个 H1 或文件名
    title = p.stem
    for line in raw.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break

    # 面包屑
    parts = path.split("/")
    crumbs = [("vault", "/vault/")]
    for i, part in enumerate(parts[:-1], 1):
        crumbs.append((part, f"/vault/?dir={'/'.join(parts[:i])}"))
    crumbs.append((parts[-1], None))
    crumb_html = " / ".join(
        f'<a href="{u}">{n}</a>' if u else f'<span>{n}</span>'
        for n, u in crumbs
    )

    page_html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title} · vault</title>
<link rel="stylesheet" href="/static/vault.css">
</head>
<body>
<aside id="sidebar">
  <header>
    <a href="/vault/" class="brand">📚 vault</a>
    <input id="search" type="search" placeholder="搜 vault..." autofocus>
  </header>
  <div id="tree">加载中...</div>
</aside>
<main>
  <nav class="crumbs">{crumb_html}</nav>
  <article class="md-body">{html}</article>
  <section id="backlinks"><h3>反向链接</h3><div id="backlinks-list">加载中...</div></section>
</main>
<script>
const PATH = {repr(path)};
fetch('/vault/api/backlinks?path=' + encodeURIComponent(PATH))
  .then(r => r.json())
  .then(d => {{
    const el = document.getElementById('backlinks-list');
    if (!d.backlinks.length) {{ el.innerHTML = '<p class="empty">无</p>'; return; }}
    el.innerHTML = d.backlinks.map(b => `
      <div class="backlink">
        <a href="/vault/page?path=${{encodeURIComponent(b.from)}}">${{b.from}}</a>
        <span class="line">:${{b.line}}</span>
        <pre>${{b.snippet}}</pre>
      </div>`).join('');
  }});
</script>
<script src="/static/vault.js" defer></script>
</body>
</html>"""
    return HTMLResponse(page_html)


@router.get("/", response_class=HTMLResponse)
def index(dir: str = Query("")):
    """vault 首页：左侧树 + 当前目录文件列表。"""
    # vault 根缺失 → 给运维友好提示而不是 500
    if not VAULT.exists() or not VAULT.is_dir():
        return HTMLResponse(
            f"""<!doctype html><html><head><meta charset=utf-8><title>vault · 配置缺失</title>
            <link rel="stylesheet" href="/static/vault.css"></head>
            <body><div style="max-width:560px;margin:80px auto;padding:0 24px">
            <h1>📚 vault 路径未配置</h1>
            <p>环境变量 <code>OBSIDIAN_VAULT_PATH</code> 指向的目录不存在：</p>
            <pre style="background:#1a1a1a;padding:12px;border-radius:4px;overflow-x:auto"><code>{VAULT}</code></pre>
            <p>请在服务器侧：</p>
            <ol>
              <li>建目录：<code>mkdir -p /data/memory-gateway/vault</code></li>
              <li>同步 vault 真源到这个目录（rsync / OSS / git）</li>
              <li>改 <code>.env</code> 设置 <code>OBSIDIAN_VAULT_PATH=/data/memory-gateway/vault</code></li>
              <li>重启容器：<code>docker compose restart</code></li>
            </ol>
            <p>详见：<a href="/vault/health">/vault/health</a> 看实时状态。</p>
            </div></body></html>""",
            status_code=503,
        )
    p = _safe_resolve(dir)
    if not p or not p.exists():
        return HTMLResponse(
            f"""<!doctype html><html><head><meta charset=utf-8><title>vault · 目录不存在</title>
            <link rel="stylesheet" href="/static/vault.css"></head>
            <body><div style="max-width:560px;margin:80px auto">
            <h1>📂 目录不存在</h1>
            <p>路径：<code>{dir}</code></p>
            <p><a href="/vault/">← 返回 vault 首页</a></p>
            </div></body></html>""",
            status_code=404,
        )
    if not p.is_dir():
        return RedirectResponse(url=f"/vault/page?path={dir}", status_code=302)

    # 当前目录文件
    files_html = ""
    for entry in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if entry.name.startswith("."):
            continue
        rel = entry.relative_to(VAULT).as_posix()
        if entry.is_dir():
            files_html += f'<li class="dir"><a href="/vault/?dir={rel}">📁 {entry.name}/</a></li>'
        elif entry.suffix.lower() in MD_EXT:
            size = entry.stat().st_size
            files_html += f'<li class="file"><a href="/vault/page?path={rel}">📄 {entry.name}</a> <span class="size">{size}B</span></li>'

    # 面包屑
    parts = [p for p in dir.split("/") if p] if dir else []
    crumbs = [("vault", "/vault/")]
    for i, part in enumerate(parts, 1):
        crumbs.append((part, f"/vault/?dir={'/'.join(parts[:i])}"))
    crumb_html = " / ".join(
        f'<a href="{u}">{n}</a>' if u else f'<span>{n}</span>'
        for n, u in crumbs
    )

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>vault · {dir or 'root'}</title>
<link rel="stylesheet" href="/static/vault.css">
</head>
<body>
<aside id="sidebar">
  <header>
    <a href="/vault/" class="brand">📚 vault</a>
    <input id="search" type="search" placeholder="搜 vault..." autofocus>
  </header>
  <div id="tree">加载中...</div>
</aside>
<main>
  <nav class="crumbs">{crumb_html}</nav>
  <h1>📂 {dir or 'vault'}</h1>
  <ul class="file-list">{files_html or '<li class="empty">空目录</li>'}</ul>
</main>
<script src="/static/vault.js" defer></script>
</body>
</html>"""
    return HTMLResponse(html)


@router.get("/health")
def vault_health():
    """vault 模块独立健康检查。"""
    if not VAULT.exists():
        return JSONResponse({"status": "missing", "vault": str(VAULT)}, status_code=500)
    md_count = sum(1 for _ in VAULT.rglob("*.md"))
    return {"status": "ok", "vault": str(VAULT), "md_files": md_count}