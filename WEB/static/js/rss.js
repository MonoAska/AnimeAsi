let rssSubscriptions = [];
let rssHistory = [];
let rssCurrentTasks = [];
const rssTaskSelection = new Set();
const rssTaskTouchedGroups = new Set();
const rssRuleFeedback = new Map();
let editingRssId = null;
let rssPreviewToken = 0;

function formatRssSourceFailures(sourceStats) {
    const failed = Array.isArray(sourceStats) ? sourceStats.filter(stat => !stat.ok) : [];
    return failed.map(stat => {
        const detail = stat.error ? `：${stat.error}` : '';
        return `${stat.name}${detail}`;
    }).join('；');
}
function showCalendarView() {
    const calendar = document.getElementById('calendarView');
    const rss = document.getElementById('rssView');
    if (calendar) calendar.style.display = '';
    if (rss) rss.style.display = 'none';
    safeCreateIcons();
}

async function openRssView() {
    const calendar = document.getElementById('calendarView');
    const rss = document.getElementById('rssView');
    if (calendar) calendar.style.display = 'none';
    if (rss) rss.style.display = '';
    await loadRssPageData();
    safeCreateIcons();
}

async function loadRssPageData() {
    try {
        const [subs, history, tasks, checkInterval] = await Promise.all([
            pywebview.api.get_rss_subscriptions(),
            pywebview.api.get_rss_download_history(80),
            pywebview.api.get_rss_current_tasks(null, 1000),
            pywebview.api.get_rss_check_interval(),
        ]);
        rssSubscriptions = Array.isArray(subs) ? subs : [];
        rssHistory = Array.isArray(history) ? history : [];
        rssCurrentTasks = Array.isArray(tasks) ? tasks : [];
        const intervalSelect = document.getElementById('rssCheckInterval');
        if (intervalSelect) intervalSelect.value = String(Number(checkInterval) || 0);
        const selectableTaskIds = new Set(
            rssCurrentTasks
                .filter(task => task.status !== 'success')
                .map(task => Number(task.id))
        );
        [...rssTaskSelection].forEach(taskId => {
            if (!selectableTaskIds.has(Number(taskId))) rssTaskSelection.delete(Number(taskId));
        });
        renderRssSubscriptionList();
        renderRssHistory();
    } catch (e) {
        showToast('RSS 订阅加载失败', 'alert-circle');
    }
}

async function setRssCheckInterval(minutes) {
    const value = Number(minutes);
    if (!Number.isFinite(value) || value < 0) return;
    try {
        const result = await pywebview.api.set_rss_check_interval(value);
        if (result && result.status === 'success') {
            const label = value > 0 ? `自动检查已设为每 ${value} 分钟` : '自动检查已关闭';
            showToast(label, 'check-circle');
            await loadRssPageData();
        } else {
            showToast((result && result.message) || '设置失败', 'alert-circle');
        }
    } catch (e) {
        showToast('设置失败', 'x-circle');
    }
}

function renderRssSubscriptionList() {
    const list = document.getElementById('rssSubscriptionList');
    const summary = document.getElementById('rssSummary');
    if (!list) return;
    const enabledCount = rssSubscriptions.filter(subscription => subscription.enabled).length;
    const pendingCount = rssCurrentTasks.filter(task => task.status !== 'success').length;
    if (summary) summary.textContent = `${rssSubscriptions.length} 个订阅，${enabledCount} 个启用，${pendingCount} 个待处理资源`;

    if (!rssSubscriptions.length) {
        list.innerHTML = `
            <div class="rss-empty">
                <div class="rss-empty-title">还没有 RSS 订阅</div>
                <div class="rss-empty-subtitle">创建一个下载计划，按关键词和过滤规则匹配资源。</div>
                <button class="btn-primary" onclick="openRssEditor()"><i data-lucide="plus" width="14" height="14"></i><span>新建订阅</span></button>
            </div>`;
        safeCreateIcons();
        return;
    }

    list.innerHTML = '';
    rssSubscriptions.forEach(subscription => {
        const card = document.createElement('div');
        card.className = `rss-card${subscription.enabled ? '' : ' disabled'}`;
        const chips = [];
        chips.push(`<span class="rss-chip ${subscription.enabled ? 'hot' : 'off'}">${subscription.enabled ? '启用' : '暂停'}</span>`);
        chips.push(`<span class="rss-chip ${subscription.auto_push ? 'hot' : 'off'}">${subscription.auto_push ? '自动推送' : '仅检查'}</span>`);
        if (subscription.search_aliases) chips.push(`<span class="rss-chip">别名: ${escapeHtml(subscription.search_aliases)}</span>`);
        if (subscription.group_filter) chips.push(`<span class="rss-chip">组: ${escapeHtml(subscription.group_filter)}</span>`);
        if (subscription.quality_filter) chips.push(`<span class="rss-chip">画质: ${escapeHtml(subscription.quality_filter)}</span>`);
        if (subscription.include_keywords) chips.push(`<span class="rss-chip">包含: ${escapeHtml(subscription.include_keywords)}</span>`);
        if (subscription.exclude_keywords) chips.push(`<span class="rss-chip">排除: ${escapeHtml(subscription.exclude_keywords)}</span>`);
        if (subscription.last_checked_at) chips.push(`<span class="rss-chip">上次: ${escapeHtml(subscription.last_checked_at)}</span>`);
        const feedback = rssRuleFeedback.get(Number(subscription.id));
        const feedbackHtml = feedback
            ? `<div class="rss-rule-feedback ${escapeHtml(feedback.type || '')}">${escapeHtml(feedback.message)}</div>`
            : '';

        card.innerHTML = `
            <div class="rss-card-head">
                <div class="rss-card-info">
                    <div class="rss-card-title">${escapeHtml(subscription.name)}</div>
                    <div class="rss-card-keyword">关键词：${escapeHtml(subscription.keyword)}</div>
                    <div class="rss-card-meta">${chips.join('')}</div>
                    <div class="rss-check-results" id="rssCheckResult_${safeNumber(subscription.id)}">${feedbackHtml}</div>
                </div>
                <div class="rss-card-actions">
                    <button class="rss-mini-btn primary" onclick="checkRssSubscription(${safeNumber(subscription.id)}, this)"><i data-lucide="radar" width="13" height="13"></i><span>检查</span></button>
                    <button class="rss-mini-btn" onclick="toggleRssSubscription(${safeNumber(subscription.id)}, ${subscription.enabled ? 'false' : 'true'})"><i data-lucide="${subscription.enabled ? 'pause' : 'play'}" width="13" height="13"></i><span>${subscription.enabled ? '暂停' : '启用'}</span></button>
                    <button class="rss-mini-btn" onclick="openRssEditor(${safeNumber(subscription.id)})"><i data-lucide="pen" width="13" height="13"></i><span>编辑</span></button>
                    <button class="rss-mini-btn danger" onclick="deleteRssSubscription(${safeNumber(subscription.id)})"><i data-lucide="trash-2" width="13" height="13"></i><span>删除</span></button>
                </div>
            </div>
            ${renderRssTaskSection(subscription.id)}
        `;
        list.appendChild(card);
    });
    safeCreateIcons();
}

function rssTasksForSubscription(subscriptionId) {
    return rssCurrentTasks.filter(task => Number(task.subscription_id) === Number(subscriptionId));
}

function rssTaskGroupKey(task) {
    const meta = task.meta || {};
    if (meta.episode) return `episode-${String(meta.episode).padStart(3, '0')}`;
    if (meta.episode_range) return `range-${String(meta.episode_range).replace(/[^0-9-]/g, '')}`;
    if (meta.is_batch) return 'batch';
    return `unknown-${safeNumber(task.id)}`;
}

function rssTaskGroupLabel(task) {
    const meta = task.meta || {};
    if (meta.episode) return `EP ${String(meta.episode).padStart(2, '0')}`;
    if (meta.episode_range) return `合集 ${escapeHtml(meta.episode_range)}`;
    if (meta.is_batch) return '合集';
    return '集数未知';
}

function rssTaskResourceScore(task) {
    const meta = task.meta || {};
    let score = task.size ? 4 : 0;
    if (meta.resolution === '1080p') score += 3;
    else if (meta.resolution === '2160p') score += 2;
    if (meta.subtitle) score += 2;
    if (meta.codec) score += 1;
    if (String(task.url || '').startsWith('magnet:')) score += 1;
    return score;
}

function groupRssTasks(subscriptionId) {
    const groups = new Map();
    rssTasksForSubscription(subscriptionId).forEach(task => {
        const key = rssTaskGroupKey(task);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(task);
    });
    const entries = [...groups.entries()];
    entries.forEach(([, tasks]) => tasks.sort((a, b) => {
        if (a.status === 'success' && b.status !== 'success') return -1;
        if (b.status === 'success' && a.status !== 'success') return 1;
        return rssTaskResourceScore(b) - rssTaskResourceScore(a);
    }));
    entries.sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }));
    return entries;
}

function ensureRssTaskDefaults(subscriptionId, groups) {
    groups.forEach(([groupKey, tasks]) => {
        const touchKey = `${subscriptionId}:${groupKey}`;
        const taskIds = new Set(tasks.map(task => Number(task.id)));
        const selected = [...rssTaskSelection].some(id => taskIds.has(Number(id)));
        const completed = tasks.some(task => task.status === 'success');
        if (!selected && !completed && !rssTaskTouchedGroups.has(touchKey)) {
            const candidate = tasks.find(task => task.status !== 'success');
            if (candidate) rssTaskSelection.add(Number(candidate.id));
        }
    });
}

function renderRssTaskSection(subscriptionId) {
    const groups = groupRssTasks(subscriptionId);
    if (!groups.length) {
        return `
            <div class="rss-task-section empty">
                <div class="rss-task-empty">尚未检查。点击“检查”后，命中的剧集会保存在这里。</div>
            </div>`;
    }
    ensureRssTaskDefaults(subscriptionId, groups);
    const tasks = groups.flatMap(([, items]) => items);
    const pending = tasks.filter(task => task.status !== 'success').length;
    const completed = tasks.length - pending;
    return `
        <div class="rss-task-section">
            <div class="rss-task-header">
                <div>
                    <div class="rss-task-title">当前任务</div>
                    <div class="rss-task-summary">${groups.length} 个剧集组 · ${pending} 个待处理 · ${completed} 个已下载</div>
                </div>
                <div class="rss-task-actions">
                    <button class="rss-mini-btn" onclick="selectBestRssTasks(${safeNumber(subscriptionId)})">选择每集最佳</button>
                    <button class="rss-mini-btn primary" onclick="pushSelectedRssTasks(${safeNumber(subscriptionId)}, this)"><i data-lucide="download" width="13" height="13"></i><span>推送所选</span></button>
                </div>
            </div>
            <div class="rss-task-groups">
                ${groups.map(([groupKey, groupTasks]) => renderRssTaskGroup(subscriptionId, groupKey, groupTasks)).join('')}
            </div>
        </div>`;
}

function renderRssTaskGroup(subscriptionId, groupKey, tasks) {
    const selectedTask = tasks.find(task => rssTaskSelection.has(Number(task.id)));
    const lead = selectedTask || tasks[0];
    const alternatives = tasks.filter(task => Number(task.id) !== Number(lead.id));
    return `
        <div class="rss-task-group">
            <div class="rss-task-group-label">${rssTaskGroupLabel(lead)}</div>
            ${renderRssTaskOption(subscriptionId, groupKey, lead)}
            ${alternatives.length ? `
                <details class="rss-task-variants">
                    <summary>其他 ${alternatives.length} 个版本</summary>
                    <div class="rss-task-variant-list">
                        ${alternatives.map(task => renderRssTaskOption(subscriptionId, groupKey, task)).join('')}
                    </div>
                </details>` : ''}
        </div>`;
}

function renderRssTaskOption(subscriptionId, groupKey, task) {
    const taskId = safeNumber(task.id);
    const checked = rssTaskSelection.has(taskId);
    const completed = task.status === 'success';
    const failed = task.status === 'error';
    const statusText = completed ? '已下载' : failed ? '失败' : '待下载';
    return `
        <label class="rss-task-option ${completed ? 'completed' : failed ? 'failed' : ''}">
            <input type="checkbox" ${checked ? 'checked' : ''} ${completed ? 'disabled' : ''}
                onchange="selectRssTask(${taskId}, ${safeNumber(subscriptionId)}, '${groupKey}', this.checked)">
            <div class="rss-task-option-main">
                <div class="rss-task-option-title" title="${escHtml(task.title)}">${escapeHtml(task.title)}</div>
                ${renderTorrentTags(task)}
                ${task.message ? `<div class="rss-task-message">${escapeHtml(task.message)}</div>` : ''}
            </div>
            <span class="rss-task-status">${statusText}</span>
        </label>`;
}

function selectRssTask(taskId, subscriptionId, groupKey, checked) {
    const touchKey = `${subscriptionId}:${groupKey}`;
    rssTaskTouchedGroups.add(touchKey);
    const groupTasks = groupRssTasks(subscriptionId)
        .find(([key]) => key === groupKey)?.[1] || [];
    groupTasks.forEach(task => rssTaskSelection.delete(Number(task.id)));
    if (checked) rssTaskSelection.add(Number(taskId));
    renderRssSubscriptionList();
}

function selectBestRssTasks(subscriptionId) {
    groupRssTasks(subscriptionId).forEach(([groupKey, tasks]) => {
        const touchKey = `${subscriptionId}:${groupKey}`;
        rssTaskTouchedGroups.add(touchKey);
        tasks.forEach(task => rssTaskSelection.delete(Number(task.id)));
        if (!tasks.some(task => task.status === 'success')) {
            const candidate = tasks.find(task => task.status !== 'success');
            if (candidate) rssTaskSelection.add(Number(candidate.id));
        }
    });
    renderRssSubscriptionList();
}

async function pushSelectedRssTasks(subscriptionId, button) {
    const validIds = new Set(
        rssTasksForSubscription(subscriptionId)
            .filter(task => task.status !== 'success')
            .map(task => Number(task.id))
    );
    const taskIds = [...rssTaskSelection].filter(id => validIds.has(Number(id)));
    if (!taskIds.length) {
        showToast('请至少选择一个待下载剧集', 'alert-circle');
        return;
    }
    if (button) {
        button.disabled = true;
        button.innerHTML = '<i data-lucide="loader-circle" width="13" height="13"></i><span>推送中</span>';
        safeCreateIcons();
    }
    rssRuleFeedback.set(Number(subscriptionId), {
        type: 'loading',
        message: `正在推送 ${taskIds.length} 个任务...`,
    });
    const feedbackBox = document.getElementById(`rssCheckResult_${safeNumber(subscriptionId)}`);
    if (feedbackBox) feedbackBox.innerHTML = `<div class="rss-rule-feedback loading">正在推送 ${taskIds.length} 个任务...</div>`;
    try {
        const response = await pywebview.api.push_rss_tasks(subscriptionId, taskIds);
        if (response.status === 'error' && !(response.failed?.length)) {
            throw new Error(response.message || '批量推送失败');
        }
        const pushedCount = response.pushed?.length || 0;
        const failedCount = response.failed?.length || 0;
        const skippedCount = response.skipped?.length || 0;
        const type = failedCount ? (pushedCount ? 'warning' : 'error') : 'success';
        rssRuleFeedback.set(Number(subscriptionId), {
            type,
            message: `批量推送完成：成功 ${pushedCount}，失败 ${failedCount}，跳过 ${skippedCount}`,
        });
        taskIds.forEach(id => rssTaskSelection.delete(Number(id)));
        await loadRssPageData();
        showToast(failedCount ? '部分任务推送失败' : `已推送 ${pushedCount} 个任务`, failedCount ? 'alert-circle' : 'check');
    } catch (error) {
        rssRuleFeedback.set(Number(subscriptionId), { type: 'error', message: '批量推送请求失败。' });
        renderRssSubscriptionList();
        showToast('批量推送请求失败', 'x-circle');
    } finally {
        if (button && document.body.contains(button)) button.disabled = false;
    }
}
function renderRssHistory() {
    const list = document.getElementById('rssHistoryList');
    if (!list) return;
    if (!rssHistory.length) {
        list.innerHTML = '<div class="rss-empty"><div class="rss-empty-title">暂无推送记录</div><div class="rss-empty-subtitle">启用自动推送后，检查命中的资源会记录在这里。</div></div>';
        return;
    }
    list.innerHTML = '';
    rssHistory.forEach(item => {
        const row = document.createElement('div');
        row.className = 'rss-history-item';
        row.innerHTML = `
            <div class="rss-history-title" title="${escHtml(item.title)}">${escapeHtml(item.title)}</div>
            <div class="rss-history-meta">
                <span class="rss-chip ${item.status === 'success' ? 'hot' : 'off'}">${escapeHtml(item.status)}</span>
                <span class="rss-chip">${escapeHtml(item.subscription_name || '订阅')}</span>
                ${item.size ? `<span class="rss-chip">${escapeHtml(item.size)}</span>` : ''}
                <span class="rss-chip">${escapeHtml(item.pushed_at || '')}</span>
            </div>
        `;
        list.appendChild(row);
    });
}

function openRssEditor(subscriptionId = null, draftKeyword = '', draftAliases = []) {
    editingRssId = subscriptionId;
    const sub = subscriptionId ? rssSubscriptions.find(s => Number(s.id) === Number(subscriptionId)) : null;
    const aliasText = Array.isArray(draftAliases) ? draftAliases.join(', ') : String(draftAliases || '');
    document.getElementById('rssEditorTitle').textContent = sub ? '编辑 RSS 订阅' : '新建 RSS 订阅';
    document.getElementById('rss_edit_id').value = sub?.id || '';
    document.getElementById('rss_edit_name').value = sub?.name || '';
    document.getElementById('rss_edit_keyword').value = sub?.keyword || draftKeyword || '';
    document.getElementById('rss_edit_aliases').value = sub?.search_aliases || aliasText;
    document.getElementById('rss_edit_group').value = sub?.group_filter || '';
    document.getElementById('rss_edit_quality').value = sub?.quality_filter || '';
    document.getElementById('rss_edit_include').value = sub?.include_keywords || '';
    document.getElementById('rss_edit_exclude').value = sub?.exclude_keywords || '';
    document.getElementById('rss_edit_path').value = sub?.save_path || '';
    setToggle('rss_edit_enabled', sub ? sub.enabled : true);
    setToggle('rss_edit_auto_push', sub ? sub.auto_push : false);
    resetRssPreview(draftKeyword ? '关键词已带入，点击“预览资源”检查真实种子。' : '填写关键词后点击“预览资源”');
    openModal('rssEditorModal');
    safeCreateIcons();
}

function setToggle(id, on) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.toggle('on', !!on);
}

function rssEditorPayload() {
    return {
        id: document.getElementById('rss_edit_id').value || null,
        name: document.getElementById('rss_edit_name').value.trim(),
        keyword: document.getElementById('rss_edit_keyword').value.trim(),
        search_aliases: document.getElementById('rss_edit_aliases').value.trim(),
        group_filter: document.getElementById('rss_edit_group').value.trim(),
        quality_filter: document.getElementById('rss_edit_quality').value.trim(),
        include_keywords: document.getElementById('rss_edit_include').value.trim(),
        exclude_keywords: document.getElementById('rss_edit_exclude').value.trim(),
        save_path: document.getElementById('rss_edit_path').value.trim(),
        enabled: document.getElementById('rss_edit_enabled').classList.contains('on'),
        auto_push: document.getElementById('rss_edit_auto_push').classList.contains('on'),
    };
}

async function saveRssEditor() {
    const payload = rssEditorPayload();
    if (!payload.keyword) {
        showToast('请填写搜索关键词', 'alert-circle');
        return;
    }
    if (!payload.name) payload.name = payload.keyword;
    try {
        const res = await pywebview.api.save_rss_subscription(payload);
        if (res.status !== 'success') {
            showToast(res.message || '保存失败', 'alert-circle');
            return;
        }
        closeModal('rssEditorModal');
        showToast('RSS 订阅已保存');
        await loadRssPageData();
    } catch (e) {
        showToast('保存请求失败', 'x-circle');
    }
}

async function toggleRssSubscription(subscriptionId, enabled) {
    try {
        const res = await pywebview.api.set_rss_subscription_enabled(subscriptionId, enabled);
        if (res.status !== 'success') {
            showToast(res.message || '切换失败', 'alert-circle');
            return;
        }
        await loadRssPageData();
    } catch (e) {
        showToast('切换失败', 'x-circle');
    }
}

function deleteRssSubscription(subscriptionId) {
    const sub = rssSubscriptions.find(item => Number(item.id) === Number(subscriptionId));
    showConfirmDialog({
        title: '删除 RSS 订阅',
        message: `确定删除订阅「${sub?.name || '未命名'}」及其全部推送记录？此操作不可撤销。`,
        confirmText: '删除',
        danger: true,
        onConfirm: async () => {
            try {
                const res = await pywebview.api.delete_rss_subscription(subscriptionId);
                if (res.status !== 'success') {
                    showToast(res.message || '删除失败', 'alert-circle');
                    return;
                }
                showToast('订阅已删除');
                await loadRssPageData();
            } catch (e) {
                showToast('删除失败', 'x-circle');
            }
        },
    });
}

async function checkRssSubscription(subscriptionId, button) {
    const id = Number(subscriptionId);
    rssRuleFeedback.set(id, { type: 'loading', message: '正在检索 RSS 源并更新当前任务...' });
    if (button) {
        button.disabled = true;
        button.innerHTML = '<i data-lucide="loader-circle" width="13" height="13"></i><span>检查中</span>';
        safeCreateIcons();
    }
    const resultBox = document.getElementById(`rssCheckResult_${safeNumber(subscriptionId)}`);
    if (resultBox) resultBox.innerHTML = '<div class="rss-rule-feedback loading">正在检索 RSS 源并更新当前任务...</div>';
    try {
        const response = await pywebview.api.check_rss_subscription(subscriptionId);
        if (response.status === 'disabled') {
            rssRuleFeedback.set(id, { type: 'warning', message: '订阅已暂停，未执行检查。' });
            renderRssSubscriptionList();
            return;
        }
        if (response.status === 'partial') {
            const failures = formatRssSourceFailures(response.source_stats);
            rssRuleFeedback.set(id, {
                type: 'warning',
                message: `检查完成，但部分源失败：${failures || '部分 RSS 源不可用'}`,
            });
            await loadRssPageData();
            showToast('部分 RSS 源连接失败', 'alert-triangle');
            return;
        }
        if (response.status !== 'success') {
            rssRuleFeedback.set(id, {
                type: 'error',
                message: `检查失败：${response.message || '请检查网络或订阅源配置'}`,
            });
            renderRssSubscriptionList();
            showToast('RSS 检查失败', 'alert-circle');
            return;
        }
        const matchedCount = response.results?.length || 0;
        const pushedCount = response.pushed?.length || 0;
        const skippedCount = response.skipped_existing || 0;
        rssRuleFeedback.set(id, {
            type: 'success',
            message: `检查完成：命中 ${matchedCount}，当前任务 ${response.task_count || 0}，自动推送 ${pushedCount}，已记录 ${skippedCount}`,
        });
        await loadRssPageData();
        showToast(`检查完成：命中 ${matchedCount} 个资源`);
    } catch (error) {
        rssRuleFeedback.set(id, { type: 'error', message: '检查请求失败，请检查网络连接。' });
        renderRssSubscriptionList();
        showToast('RSS 检查请求失败', 'x-circle');
    } finally {
        if (button && document.body.contains(button)) {
            button.disabled = false;
            button.innerHTML = '<i data-lucide="radar" width="13" height="13"></i><span>检查</span>';
            safeCreateIcons();
        }
    }
}
function resetRssPreview(message) {
    rssPreviewToken++;
    const summary = document.getElementById('rssPreviewSummary');
    const list = document.getElementById('rssPreviewList');
    if (summary) summary.textContent = message;
    if (list) list.innerHTML = `
        <div class="rss-preview-empty">
            <i data-lucide="search" width="22" height="22"></i>
            <span>这里会展示真实 RSS 资源，以及当前规则的匹配结果。</span>
        </div>`;
}

async function startRssDraftFromDownload() {
    const context = getCurrentTorrentSearchContext();
    const keyword = context.request?.keyword
        || document.getElementById('dlModalTitle')?.textContent?.trim()
        || '';
    if (!keyword) {
        showToast('当前番剧缺少可用关键词', 'alert-circle');
        return;
    }
    let keywords = Array.isArray(context.keywords) ? context.keywords : [keyword];
    if (context.request) {
        try {
            const resolved = await pywebview.api.get_torrent_search_keywords(context.request);
            if (Array.isArray(resolved) && resolved.length) keywords = resolved;
        } catch (e) {
            console.warn('获取搜索别名失败:', e);
        }
    }
    const aliases = keywords.filter(value => value.toLocaleLowerCase() !== keyword.toLocaleLowerCase());
    closeModal('dlModal');
    await openRssView();
    openRssEditor(null, keyword, aliases);
}

function setRssPreviewLoading(loading) {
    const button = document.getElementById('rssPreviewBtn');
    if (!button) return;
    button.disabled = loading;
    button.innerHTML = loading
        ? '<i data-lucide="loader-circle" width="13" height="13"></i><span>检索中</span>'
        : '<i data-lucide="search" width="13" height="13"></i><span>预览资源</span>';
    safeCreateIcons();
}

async function previewRssRules() {
    const payload = rssEditorPayload();
    if (!payload.keyword) {
        showToast('请先填写搜索关键词', 'alert-circle');
        return;
    }
    const token = ++rssPreviewToken;
    const summary = document.getElementById('rssPreviewSummary');
    const list = document.getElementById('rssPreviewList');
    if (summary) summary.textContent = '正在检索启用的 RSS 源...';
    if (list) list.innerHTML = '<div class="rss-preview-loading"><div class="season-spinner"></div><span>正在获取真实资源</span></div>';
    setRssPreviewLoading(true);
    try {
        const response = await pywebview.api.preview_rss_subscription(payload);
        if (token !== rssPreviewToken) return;
        renderRssPreview(response);
    } catch (e) {
        if (token !== rssPreviewToken) return;
        renderRssPreview({ status: 'error', message: '预览请求失败，请检查网络连接。', results: [] });
    } finally {
        if (token === rssPreviewToken) setRssPreviewLoading(false);
    }
}

function renderRssPreview(response) {
    const summary = document.getElementById('rssPreviewSummary');
    const list = document.getElementById('rssPreviewList');
    if (!list) return;
    const results = Array.isArray(response?.results) ? response.results : [];
    if (response?.status === 'error') {
        if (summary) summary.textContent = '预览失败';
        list.innerHTML = `<div class="rss-preview-empty error"><i data-lucide="circle-alert" width="22" height="22"></i><span>${escapeHtml(response.message || 'RSS 源连接失败')}</span></div>`;
        safeCreateIcons();
        return;
    }
    const partialNote = response?.status === 'partial'
        ? formatRssSourceFailures(response.source_stats)
        : '';
    if (!results.length) {
        if (summary) {
            summary.textContent = partialNote
                ? `未找到相关资源 · 部分源失败：${partialNote}`
                : '未找到相关资源';
        }
        list.innerHTML = '<div class="rss-preview-empty"><i data-lucide="search-x" width="22" height="22"></i><span>这些关键词没有返回可下载种子，请尝试其他中文译名、罗马字或更短的关键词。</span></div>';
        safeCreateIcons();
        return;
    }
    const matchedCount = results.filter(item => item.matched).length;
    const searched = Array.isArray(response?.keywords) ? response.keywords.join(` / `) : ``;
    if (summary) {
        summary.textContent = `找到 ${results.length} 个，匹配 ${matchedCount} 个`
            + `${partialNote ? ` · 部分源失败：${partialNote}` : ``}`
            + `${searched ? ` · ${searched}` : ``}`;
    }
    list.innerHTML = results.slice(0, 40).map(renderRssPreviewItem).join('');
    safeCreateIcons();
}

function renderRssPreviewItem(item) {
    const matched = !!item.matched;
    const reasons = Array.isArray(item.reasons) ? item.reasons : [];
    const meta = item.meta || {};
    const actions = [];
    if (meta.group) actions.push(`<button onclick="applyRssPreviewValue('rss_edit_group','${escAttr(meta.group)}')">使用字幕组</button>`);
    if (meta.resolution) actions.push(`<button onclick="applyRssPreviewValue('rss_edit_quality','${escAttr(meta.resolution)}')">使用 ${escapeHtml(meta.resolution)}</button>`);
    [meta.subtitle, meta.codec].filter(Boolean).forEach(value => {
        actions.push(`<button onclick="applyRssPreviewValue('rss_edit_include','${escAttr(value)}')">包含 ${escapeHtml(value)}</button>`);
    });
    return `
        <div class="rss-preview-item ${matched ? 'matched' : 'rejected'}">
            <div class="rss-preview-item-head">
                <span class="rss-preview-status">${matched ? '匹配' : '排除'}</span>
                <span class="rss-preview-source">${escapeHtml(item.source || '')}</span>
            </div>
            <div class="rss-preview-item-title" title="${escHtml(item.title)}">${escapeHtml(item.title)}</div>
            ${renderTorrentTags(item)}
            ${reasons.length ? `<div class="rss-preview-reasons">${reasons.map(reason => escapeHtml(reason)).join('；')}</div>` : ''}
            ${actions.length ? `<div class="rss-preview-actions">${actions.join('')}</div>` : ''}
        </div>`;
}

function applyRssPreviewValue(fieldId, value) {
    const input = document.getElementById(fieldId);
    const cleanValue = String(value || '').trim();
    if (!input || !cleanValue) return;
    const values = input.value.split(/[,，]/).map(item => item.trim()).filter(Boolean);
    if (!values.some(item => item.toLowerCase() === cleanValue.toLowerCase())) {
        values.push(cleanValue);
        input.value = values.join(', ');
    }
    input.focus();
}
document.addEventListener('DOMContentLoaded', () => {
    const rssBtn = document.getElementById('rssBtn');
    if (rssBtn) rssBtn.onclick = openRssView;
    const keywordInput = document.getElementById('rss_edit_keyword');
    if (keywordInput) keywordInput.addEventListener('keydown', event => {
        if (event.key === 'Enter') previewRssRules();
    });
    const logo = document.querySelector('.logo');
    if (logo) logo.onclick = (event) => {
        event.preventDefault();
        showCalendarView();
    };
});

window.showCalendarView = showCalendarView;
window.openRssView = openRssView;
window.loadRssPageData = loadRssPageData;
window.openRssEditor = openRssEditor;
window.saveRssEditor = saveRssEditor;
window.toggleRssSubscription = toggleRssSubscription;
window.deleteRssSubscription = deleteRssSubscription;
window.checkRssSubscription = checkRssSubscription;
window.startRssDraftFromDownload = startRssDraftFromDownload;
window.previewRssRules = previewRssRules;
window.applyRssPreviewValue = applyRssPreviewValue;
window.selectRssTask = selectRssTask;
window.selectBestRssTasks = selectBestRssTasks;
window.pushSelectedRssTasks = pushSelectedRssTasks;
window.setRssCheckInterval = setRssCheckInterval;
