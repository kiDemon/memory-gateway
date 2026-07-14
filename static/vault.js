/* vault.js — Obsidian vault 浏览器：树渲染 + 搜索 + 高亮 */
(function () {
  'use strict';

  const TREE_ENDPOINT = '/vault/api/tree';
  const SEARCH_ENDPOINT = '/vault/api/search';

  // ── Tree 渲染 ──────────────────────────────────────────
  function renderTree(items, container, depth) {
    depth = depth || 0;
    items.forEach(item => {
      const row = document.createElement('div');
      row.className = 'tree-item';
      row.dataset.path = item.path;
      row.dataset.type = item.type;

      const toggle = document.createElement('span');
      toggle.className = 'tree-toggle';
      toggle.textContent = item.type === 'dir' ? '▸' : ' ';
      row.appendChild(toggle);

      const icon = document.createElement('span');
      icon.className = 'tree-icon';
      icon.textContent = item.type === 'dir' ? '📁' : '📄';
      row.appendChild(icon);

      const name = document.createElement('span');
      name.className = 'tree-name';
      name.textContent = item.name;
      row.appendChild(name);

      row.addEventListener('click', (e) => {
        e.stopPropagation();
        // 标记 active
        document.querySelectorAll('.tree-item.active').forEach(el => el.classList.remove('active'));
        row.classList.add('active');

        if (item.type === 'dir') {
          // toggle 子目录
          const child = container.querySelector(`[data-parent="${CSS.escape(item.path)}"]`);
          if (child) {
            const open = child.style.display !== 'none';
            child.style.display = open ? 'none' : 'block';
            toggle.textContent = open ? '▸' : '▾';
          } else {
            renderTree(item.children || [], container, depth + 1).then(el => {
              el.dataset.parent = item.path;
              row.parentNode.insertBefore(el, row.nextSibling);
              toggle.textContent = '▾';
            });
          }
        } else {
          // 文件 → 导航到 page
          window.location.href = '/vault/page?path=' + encodeURIComponent(item.path);
        }
      });

      container.appendChild(row);
    });
    return Promise.resolve(container);
  }

  // ── 加载树 ──────────────────────────────────────────────
  async function loadTree() {
    const container = document.getElementById('tree');
    if (!container) return;
    try {
      const res = await fetch(TREE_ENDPOINT);
      const data = await res.json();
      container.innerHTML = '';
      await renderTree(data.tree, container);
    } catch (err) {
      container.innerHTML = `<p class="empty" style="padding:0 16px">加载失败: ${err.message}</p>`;
    }
  }

  // ── 搜索 ───────────────────────────────────────────────
  let searchTimer = null;
  function setupSearch() {
    const input = document.getElementById('search');
    if (!input) return;

    input.addEventListener('input', () => {
      clearTimeout(searchTimer);
      const q = input.value.trim();
      if (!q) {
        // 清空搜索，恢复树
        loadTree();
        return;
      }
      searchTimer = setTimeout(async () => {
        try {
          const res = await fetch(SEARCH_ENDPOINT + '?q=' + encodeURIComponent(q) + '&limit=50');
          const data = await res.json();
          renderSearchResults(data.hits, q);
        } catch (err) {
          console.error('search failed', err);
        }
      }, 200);
    });

    // Enter → 跳到首个结果
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const first = document.querySelector('.search-result');
        if (first) first.click();
      }
    });
  }

  function renderSearchResults(hits, q) {
    const container = document.getElementById('tree');
    if (!container) return;
    container.innerHTML = '';
    if (!hits.length) {
      container.innerHTML = `<p class="empty" style="padding:0 16px">未找到 "${escapeHtml(q)}"</p>`;
      return;
    }
    hits.forEach(hit => {
      const div = document.createElement('div');
      div.className = 'search-result';
      div.innerHTML = `
        <div class="path">${escapeHtml(hit.path)}</div>
        <div class="snippet">${escapeHtml(hit.snippet)}</div>`;
      div.addEventListener('click', () => {
        window.location.href = '/vault/page?path=' + encodeURIComponent(hit.path);
      });
      container.appendChild(div);
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  // ── boot ───────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', () => {
    loadTree();
    setupSearch();
  });
})();