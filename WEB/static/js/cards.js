const CARD_HEART_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>`;
const CARD_PLAY_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><polygon points="8,5 19,12 8,19"/></svg>`;
const LOCAL_COVER_PLACEHOLDER = 'data:image/svg+xml;charset=UTF-8,' + encodeURIComponent(`
<svg xmlns="http://www.w3.org/2000/svg" width="120" height="162" viewBox="0 0 120 162">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#25283a"/>
      <stop offset="1" stop-color="#151722"/>
    </linearGradient>
  </defs>
  <rect width="120" height="162" rx="8" fill="url(#g)"/>
  <rect x="28" y="42" width="64" height="78" rx="6" fill="none" stroke="#7c6dfa" stroke-width="3" opacity=".55"/>
  <path d="M42 98l14-17 12 13 8-9 14 18" fill="none" stroke="#ff6b9d" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" opacity=".8"/>
  <circle cx="72" cy="61" r="7" fill="#7c6dfa" opacity=".8"/>
  <text x="60" y="136" fill="#8b90a8" font-family="Arial, sans-serif" font-size="10" text-anchor="middle">No Cover</text>
</svg>`);

function coverSrc(value) {
    return value || LOCAL_COVER_PLACEHOLDER;
}

function handleCoverError(img) {
    if (!img || img.dataset.fallback === '1') return;
    img.dataset.fallback = '1';
    img.classList.add('is-fallback');
    img.src = LOCAL_COVER_PLACEHOLDER;
}

function createAnimeCard(item, options = {}) {
    const name = options.name ?? (item.display_name || item.name_cn || item.name || '');
    const img = options.img ?? (item.images?.common || item.images?.large || item.img || '');
    const url = options.url ?? (item.url || (item.id ? `https://bgm.tv/subject/${item.id}` : ''));
    const rating = options.rating ?? item.rating;
    const rank = options.rank ?? item.rank;
    const tags = options.tags ?? item.top_tags;
    const popularityClass = popularLevel(rating, rank);
    const tierClass = popularityClass ? ' ' + popularityClass : '';
    const ratingHtml = rating?.score
        ? `<div class="rating-badge${tierClass}">★ ${rating.score.toFixed(1)}</div>`
        : '';
    const tagsHtml = tags?.length
        ? `<div class="card-tags">${tags.map(t => `<span class="tag-chip">${escapeHtml(t)}</span>`).join('')}</div>`
        : '';
    const isFav = options.forceLiked ?? globalFavorites.some(f => f.name === name);
    const heartClass = isFav ? 'heart-btn liked' : 'heart-btn';
    const heartClick = options.heartClick || `handleToggleFav(event, ${safeNumber(item.id)}, '${escAttr(name)}', '${escAttr(img)}', '${escAttr(url)}', ${safeNumber(rating?.score)}, ${safeNumber(rank)}, this)`;
    const playOverlay = options.playName
        ? `<div class="play-btn-overlay" onclick="event.stopPropagation(); openEpisodeModal('${escAttr(options.playName)}')"><div class="play-circle">${CARD_PLAY_SVG}</div></div>`
        : '';

    const card = document.createElement('div');
    card.className = `anime-card${popularityClass ? ' ' + popularityClass : ''}`;
    card.innerHTML = `
        <div class="card-poster" onclick="event.stopPropagation(); openDetailModal(${safeNumber(item.id)})" style="cursor:pointer">
            <img loading="lazy" decoding="async" src="${escHtml(coverSrc(img))}" onerror="handleCoverError(this)">
            ${ratingHtml}
            ${playOverlay}
            <div class="card-overlay" style="pointer-events:none">
                <div class="card-overlay-title">${escapeHtml(name)}</div>
                ${tagsHtml}
            </div>
            <div class="${heartClass}" onclick="${heartClick}">${CARD_HEART_SVG}</div>
        </div>
        <div class="card-body">
            <button class="card-btn btn-detail" onclick="openLink('${escAttr(url)}')"><i data-lucide="external-link"></i><span>详情</span></button>
            <button class="card-btn btn-dl" onclick="handleSubjectSearch(${safeNumber(item.id)}, '${escAttr(name)}')"><i data-lucide="download"></i><span>下载</span></button>
        </div>
    `;
    return card;
}

function replaceGridCards(grid, fragment) {
    const token = (grid._replaceToken || 0) + 1;
    grid._replaceToken = token;
    grid.classList.remove('is-fading-in');
    grid.classList.add('is-updating', 'is-fading-out');

    const commit = () => {
        if (grid._replaceToken !== token) return;
        grid.replaceChildren(fragment);
        grid.classList.remove('is-fading-out');
        grid.classList.add('is-fading-in');
        requestAnimationFrame(() => {
            if (grid._replaceToken !== token) return;
            safeCreateIcons();
            window.setTimeout(() => {
                if (grid._replaceToken !== token) return;
                grid.classList.remove('is-updating', 'is-fading-in');
            }, 170);
        });
    };

    if (grid.childElementCount === 0 || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        commit();
    } else {
        window.setTimeout(commit, 90);
    }
}

window.coverSrc = coverSrc;
window.handleCoverError = handleCoverError;
window.createAnimeCard = createAnimeCard;
window.replaceGridCards = replaceGridCards;
