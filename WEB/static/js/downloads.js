let torrentSearchToken = 0;
let currentTorrentSearchRequest = null;
let currentTorrentSearchKeywords = [];
const torrentSearchCache = new Map();
const TORRENT_CACHE_TTL = 60 * 1000;

function normalizeTorrentSearchRequest(input) {
    if (input && typeof input === 'object') {
        return {
            keyword: String(input.keyword || '').trim(),
            subject_id: safeNumber(input.subject_id),
        };
    }
    return { keyword: String(input || '').trim(), subject_id: 0 };
}

function torrentRequestKey(request) {
    return `${request.subject_id || 0}:${request.keyword.toLocaleLowerCase()}`;
}

function handleSubjectSearch(subjectId, keyword) {
    return handleSearch({ subject_id: subjectId, keyword });
}

async function handleSearch(input) {
    const fallback = document.getElementById('globalSearch')?.value || '';
    const request = normalizeTorrentSearchRequest(input || fallback);
    if (!request.keyword) return;

    const token = ++torrentSearchToken;
    currentTorrentSearchRequest = request;
    currentTorrentSearchKeywords = [request.keyword];
    openDownloadModal(request.keyword);
    const cached = getCachedTorrentResults(request);
    if (cached) {
        currentTorrentSearchKeywords = cached.keywords;
        renderTorrentList(request.keyword, cached.results, cached.keywords, true);
        return;
    }

    renderTorrentState('searching', '正在搜索资源', '正在并行检索中文名和官方罗马字别名。');
    try {
        const res = await pywebview.api.search_torrents(request);
        if (token !== torrentSearchToken) return;
        const keywords = Array.isArray(res.keywords) && res.keywords.length
            ? res.keywords
            : [request.keyword];
        currentTorrentSearchKeywords = keywords;
        if (res.status === 'success' || res.status === 'partial') {
            setCachedTorrentResults(request, res.results, keywords);
            renderTorrentList(request.keyword, res.results, keywords);
            if (res.status === 'partial') showToast('部分 RSS 源连接失败', 'alert-triangle');
        } else if (res.status === 'empty') {
            updateDownloadSearchSubtitle(keywords);
            renderTorrentState('empty', '没有找到可用资源', '已尝试中文名和官方罗马字别名，可以在 RSS 编辑器中调整关键词。');
        } else {
            updateDownloadSearchSubtitle(keywords);
            renderTorrentState('error', '搜索失败', res.message || '请检查网络、代理或订阅源配置。');
        }
    } catch (e) {
        if (token !== torrentSearchToken) return;
        renderTorrentState('error', '搜索请求失败', '请检查网络连接或代理设置后重试。');
    }
}

function retryTorrentSearch() {
    if (currentTorrentSearchRequest) handleSearch(currentTorrentSearchRequest);
}

function openDownloadModal(animeName) {
    const list = document.getElementById('torrentList');
    document.getElementById('dlModalTitle').textContent = animeName;
    updateDownloadSearchSubtitle([animeName], true);
    pywebview.api.get_init_config().then(conf => {
        const safeName = animeName.replace(/[\\/:*?"<>|]/g, '_');
        document.getElementById('dlPath').value = (conf.local_anime_path || "E:\\ANIME") + "\\" + safeName;
    }).catch(e => console.warn('获取配置失败:', e));
    if (list) list.innerHTML = '';
    openModal('dlModal');
}

function updateDownloadSearchSubtitle(keywords, searching = false) {
    const subtitle = document.getElementById('dlModalSubtitle');
    if (!subtitle) return;
    const values = Array.isArray(keywords) ? keywords.filter(Boolean) : [];
    subtitle.textContent = searching
        ? '正在解析中文名与官方罗马字别名...'
        : `搜索词：${values.join(' / ')}`;
}

function getCachedTorrentResults(request) {
    const key = torrentRequestKey(request);
    const cache = torrentSearchCache.get(key);
    if (!cache || Date.now() - cache.time > TORRENT_CACHE_TTL) {
        torrentSearchCache.delete(key);
        return null;
    }
    return { results: cache.results, keywords: cache.keywords };
}

function setCachedTorrentResults(request, results, keywords) {
    torrentSearchCache.set(torrentRequestKey(request), {
        time: Date.now(),
        results,
        keywords,
    });
}

function getCurrentTorrentSearchContext() {
    return {
        request: currentTorrentSearchRequest,
        keywords: [...currentTorrentSearchKeywords],
    };
}

function renderTorrentState(type, title, subtitle) {
    const list = document.getElementById('torrentList');
    if (!list) return;
    const spinner = type === 'searching' ? '<div class="season-spinner" aria-hidden="true"></div>' : '';
    const retry = type === 'error'
        ? '<button class="btn-primary" onclick="retryTorrentSearch()">重试</button>'
        : '';
    list.innerHTML = `
        <div class="torrent-state" role="${type === 'error' ? 'alert' : 'status'}" aria-live="polite">
            ${spinner}
            <div class="torrent-state-title">${escapeHtml(title)}</div>
            <div class="torrent-state-subtitle">${escapeHtml(subtitle || '')}</div>
            ${retry}
        </div>
    `;
}

function renderTorrentList(animeName, results, keywords, fromCache = false) {
    const list = document.getElementById('torrentList');
    document.getElementById('dlModalTitle').textContent = animeName;
    updateDownloadSearchSubtitle(keywords);
    if (!list) return;
    list.innerHTML = '';
    if (!results || results.length === 0) {
        renderTorrentState('empty', '没有找到可用资源', '已尝试中文名和官方罗马字别名。');
        return;
    }
    if (fromCache) showToast('已使用刚才的搜索结果');

    results.forEach(t => {
        const row = document.createElement('div');
        row.className = 'torrent-row';
        row.innerHTML = `
            <div class="torrent-info">
                <div class="torrent-name" title="${escHtml(t.title)}">${escapeHtml(t.title)}</div>
                ${renderTorrentTags(t)}
            </div>
            <button class="btn-push" onclick="executePush('${escAttr(t.url)}', '${escAttr(animeName)}', this)">推送</button>
        `;
        list.appendChild(row);
    });
}

function renderTorrentTags(t) {
    const tags = Array.isArray(t.resource_tags) ? [...t.resource_tags] : [];
    if (t.size) {
        tags.push({ text: t.size, isSize: true });
    }
    if (!tags.length) return '<div class="torrent-tags"><span class="torrent-tag unknown">集数未知</span></div>';
    return `<div class="torrent-tags">${tags.map(tag => {
        if (tag && tag.isSize) {
            return `<span class="torrent-tag size" title="文件大小">${escapeHtml(tag.text)}</span>`;
        }
        const tagText = String(tag || '');
        const cls = tagText === '合集'
            ? 'torrent-tag batch'
            : tagText === '集数未知' ? 'torrent-tag unknown' : 'torrent-tag';
        return `<span class="${cls}" title="${escHtml(tagText)}">${escapeHtml(tagText)}</span>`;
    }).join('')}</div>`;
}

async function executePush(url, name, btn) {
    const path = document.getElementById('dlPath').value;
    btn.disabled = true;
    btn.textContent = '传送中...';

    try {
        const res = await pywebview.api.push_download(url, name, path);
        if (res.status === 'success') {
            btn.textContent = '✓ 已发送';
            btn.style.background = '#2e7d32';
            showToast('已推送到 qBittorrent');
        } else {
            btn.disabled = false;
            btn.textContent = '重试';
            showToast('失败: ' + res.message, 'alert-triangle');
        }
    } catch (e) {
        btn.disabled = false;
        btn.textContent = '重试';
        showToast('连接失败', 'x-circle');
    }
}

window.handleSearch = handleSearch;
window.handleSubjectSearch = handleSubjectSearch;
window.retryTorrentSearch = retryTorrentSearch;
window.getCurrentTorrentSearchContext = getCurrentTorrentSearchContext;
window.openDownloadModal = openDownloadModal;
window.renderTorrentState = renderTorrentState;
window.renderTorrentList = renderTorrentList;
window.renderTorrentTags = renderTorrentTags;
window.executePush = executePush;
