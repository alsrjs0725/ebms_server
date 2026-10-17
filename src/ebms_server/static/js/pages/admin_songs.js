// ---- 탭 ----
document.querySelectorAll("[data-tab]").forEach((btn) => btn.addEventListener("click", () => {
    document.querySelectorAll("[data-tab]").forEach((b) => {
        const on = b === btn;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", on);
        document.getElementById(`tab-${b.dataset.tab}`).classList.toggle("d-none", !on);
    });
    history.replaceState(null, "", btn.dataset.url);
}));

// ---- 곡 목록 ----
const songRows = document.getElementById("song-rows");
const songDetail = document.getElementById("song-detail");
let songQuery = "";
let songPage = 1;
let currentSong = null;

function size(n) {
    if (n < 1024) return `${n}B`;
    if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)}KB`;
    if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)}MB`;
    return `${(n / 1024 ** 3).toFixed(2)}GB`;
}

// 쪽 번호 목록: 처음·이전·현재 앞뒤 2쪽·다음·끝
function renderPager(nav, data, go) {
    nav.replaceChildren();
    if (data.pages <= 1) return;
    const ul = document.createElement("ul");
    ul.className = "pagination pagination-sm flex-wrap mb-1";
    const item = (label, page, { active = false, disabled = false, aria } = {}) => {
        const li = document.createElement("li");
        li.className = "page-item" + (active ? " active" : "") + (disabled ? " disabled" : "");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "page-link";
        btn.textContent = label;
        if (aria) btn.setAttribute("aria-label", aria);
        if (active) btn.setAttribute("aria-current", "page");
        if (!active && !disabled) btn.addEventListener("click", () => go(page));
        li.append(btn);
        ul.append(li);
    };
    const { page, pages } = data;
    item("«", 1, { disabled: page === 1, aria: "첫 쪽" });
    item("‹", page - 1, { disabled: page === 1, aria: "이전 쪽" });
    const from = Math.max(1, Math.min(page - 2, pages - 4));
    const to = Math.min(pages, from + 4);
    for (let p = from; p <= to; p++) item(String(p), p, { active: p === page });
    item("›", page + 1, { disabled: page === pages, aria: "다음 쪽" });
    item("»", pages, { disabled: page === pages, aria: "끝 쪽" });
    const info = document.createElement("div");
    info.className = "text-muted small";
    info.textContent = `${page} / ${pages}쪽 · 모두 ${data.total}개`;
    nav.append(ul, info);
}

async function loadSongs(page = 1) {
    const res = await fetch(`/api/admin/songs?${new URLSearchParams({ q: songQuery, page })}`);
    if (!res.ok) { alert(await detail(res)); return; }
    const data = await res.json();
    if (page > data.pages) return loadSongs(data.pages);  // 마지막 쪽 곡을 사용 중지한 경우
    songPage = data.page;
    songRows.replaceChildren();
    for (const song of data.items) {
        const tr = document.createElement("tr");
        tr.style.cursor = "pointer";
        tr.dataset.id = song.id;
        tr.tabIndex = 0;
        tr.classList.toggle("table-active", currentSong?.id === song.id);
        for (const [text, cls] of [[song.id, ""], [song.folder || "(이름 없음)", "text-break"], [song.charts, "text-end"], [size(song.size), "text-end text-nowrap"]]) {
            const td = document.createElement("td");
            td.className = cls;
            td.textContent = text;
            tr.append(td);
        }
        tr.addEventListener("click", () => openSong(song.id));
        tr.addEventListener("keydown", (e) => { if (e.key === "Enter") openSong(song.id); });
        songRows.append(tr);
    }
    document.getElementById("song-empty").classList.toggle("d-none", data.items.length > 0);
    renderPager(document.getElementById("song-pager"), data, loadSongs);
}

document.getElementById("song-search").addEventListener("submit", (e) => {
    e.preventDefault();
    songQuery = document.getElementById("song-q").value.trim();
    loadSongs(1);
});

// ---- 사용 중지한 곡 ----
const deletedRows = document.getElementById("deleted-rows");
let deletedQuery = "";
let deletedPage = 1;
let deletedLoaded = false;

async function loadDeleted(page = 1) {
    const res = await fetch(`/api/admin/songs/deleted?${new URLSearchParams({ q: deletedQuery, page })}`);
    if (!res.ok) { alert(await detail(res)); return; }
    const data = await res.json();
    if (page > data.pages) return loadDeleted(data.pages);  // 마지막 쪽 곡을 복원한 경우
    deletedPage = data.page;
    deletedRows.replaceChildren();
    for (const row of data.items) {
        const tr = document.createElement("tr");
        const when = new Date(row.deleted_at * 1000).toLocaleString();
        const who = row.deleted_by_name || row.deleted_by || "";
        for (const [text, cls] of [
            [row.song_id, ""],
            [`${row.folder || "(이름 없음)"} · 차트 ${row.charts}개 · ${size(row.size)}`, "text-break"],
            [row.reason || "(사유 없음)", "text-break" + (row.reason ? "" : " text-muted")],
            [`${when}${who ? " · " + who : ""}`, "text-nowrap"],
        ]) {
            const td = document.createElement("td");
            td.className = cls;
            td.textContent = text;
            tr.append(td);
        }
        const td = document.createElement("td");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-sm btn-outline-primary text-nowrap";
        btn.textContent = "복원";
        btn.addEventListener("click", () => restoreSong(row, btn));
        td.append(btn);
        tr.append(td);
        deletedRows.append(tr);
    }
    document.getElementById("deleted-empty").classList.toggle("d-none", data.items.length > 0);
    renderPager(document.getElementById("deleted-pager"), data, loadDeleted);
    deletedLoaded = true;
}

async function restoreSong(row, btn) {
    if (!confirm(`#${row.song_id} ${row.folder} 곡을 같은 번호로 복원합니다.`)) return;
    btn.disabled = true;
    try {
        const res = await fetch(`/api/admin/songs/${row.song_id}/restore`, { method: "POST" });
        if (!res.ok) { alert(await detail(res)); return; }
        loadDeleted(deletedPage);
        loadSongs(songPage);
    } finally {
        btn.disabled = false;
    }
}

document.getElementById("deleted-search").addEventListener("submit", (e) => {
    e.preventDefault();
    deletedQuery = document.getElementById("deleted-q").value.trim();
    loadDeleted(1);
});
// 탭을 처음 열 때, 그리고 곡을 사용 중지한 뒤 다시 열 때 새로 읽습니다.
document.querySelector('[data-tab="deleted"]').addEventListener("click", () => { if (!deletedLoaded) loadDeleted(deletedPage); });

// ---- 파일 구조 ----
// 파일의 종류: kind(play/pre)와, 차트면 chart. 체크한 종류에 하나라도 속하면 보입니다(종류가 없는 파일은 항상 보임).
const fileFilter = document.getElementById("file-filter");

function fileCategories(file) {
    const cats = [];
    if (file.kind) cats.push(file.kind);
    if (file.chart) cats.push("chart");
    return cats;
}

function visibleFiles(files) {
    const shown = new Set([...fileFilter.querySelectorAll("input:checked")].map((el) => el.value));
    return files.filter((f) => {
        const cats = fileCategories(f);
        return !cats.length || cats.some((c) => shown.has(c));
    });
}

function renderTree() {
    const song = currentSong;
    const box = document.getElementById("song-tree");
    // 펼쳐 둔 폴더는 필터를 바꿔도 유지합니다.
    const opened = new Set([...box.querySelectorAll("details[open]")].map((d) => d.dataset.path));
    const files = visibleFiles(song.files);
    if (!files.length) {
        box.innerHTML = '<p class="text-muted mb-0">보일 파일이 없습니다.</p>';
        return;
    }
    const tree = renderNode(fileTree(files), song.id, "");
    tree.classList.remove("ms-3");
    const details = tree.querySelectorAll("details");
    details.forEach((d) => { if (opened.has(d.dataset.path)) d.open = true; });
    // 처음 열 때 최상위 폴더 하나만 있으면 펼쳐 둡니다.
    const top = tree.querySelectorAll(":scope > li > details");
    if (!opened.size && top.length === 1) top[0].open = true;
    box.replaceChildren(tree);
}

fileFilter.addEventListener("change", () => { if (currentSong) renderTree(); });

function fileTree(files) {
    // "a/b/c.wav" 경로를 폴더별로 묶습니다.
    const root = { dirs: new Map(), files: [] };
    for (const file of files) {
        const parts = file.path.split("/");
        let node = root;
        for (const part of parts.slice(0, -1)) {
            if (!node.dirs.has(part)) node.dirs.set(part, { dirs: new Map(), files: [] });
            node = node.dirs.get(part);
        }
        node.files.push({ ...file, name: parts[parts.length - 1] });
    }
    return root;
}

function nodeSize(node) {
    let total = node.files.reduce((sum, f) => sum + f.size, 0);
    let count = node.files.length;
    for (const dir of node.dirs.values()) {
        const [s, c] = nodeSize(dir);
        total += s;
        count += c;
    }
    return [total, count];
}

function renderNode(node, songId, base) {
    const ul = document.createElement("ul");
    ul.className = "list-unstyled ms-3 mb-0";
    const byName = (a, b) => a.localeCompare(b);
    for (const name of [...node.dirs.keys()].sort(byName)) {
        const dir = node.dirs.get(name);
        const [total, count] = nodeSize(dir);
        const li = document.createElement("li");
        const details = document.createElement("details");
        details.dataset.path = base + name + "/";
        const summary = document.createElement("summary");
        summary.textContent = `📁 ${name}/ `;
        const meta = document.createElement("span");
        meta.className = "text-muted";
        meta.textContent = `(${count}개, ${size(total)})`;
        summary.append(meta);
        details.append(summary, renderNode(dir, songId, base + name + "/"));
        li.append(details);
        ul.append(li);
    }
    for (const file of node.files.sort((a, b) => byName(a.name, b.name))) {
        const li = document.createElement("li");
        li.className = "d-flex gap-2 align-items-baseline";
        const a = document.createElement("a");
        a.href = `/api/admin/songs/${songId}/file?path=${encodeURIComponent(file.path)}`;
        a.className = "text-break";
        a.textContent = file.name;
        a.title = `${file.path} 받기`;
        const meta = document.createElement("span");
        meta.className = "text-muted text-nowrap";
        meta.textContent = size(file.size);
        li.append(a, meta);
        if (file.chart) {
            const badge = document.createElement("span");
            badge.className = "badge text-bg-primary";
            badge.textContent = "차트";
            li.append(badge);
        }
        if (file.kind) {
            const badge = document.createElement("span");
            badge.className = "badge " + (file.kind === "pre" ? "text-bg-info" : "text-bg-light border");
            badge.textContent = file.kind === "pre" ? "사전" : "플레이";
            badge.title = file.kind === "pre" ? "사전 다운로드 파일(배너·프리뷰 등)" : "플레이할 때 받는 파일";
            li.append(badge);
        }
        ul.append(li);
    }
    return ul;
}

async function openSong(id) {
    const res = await fetch(`/api/admin/songs/${id}`);
    if (!res.ok) { alert(await detail(res)); return; }
    const song = await res.json();
    currentSong = song;
    songRows.querySelectorAll("tr").forEach((tr) => tr.classList.toggle("table-active", Number(tr.dataset.id) === id));
    document.getElementById("song-title").textContent = `#${song.id} ${song.folder || "(이름 없음)"}`;
    document.getElementById("song-meta").textContent =
        `zip ${size(song.size)} · 파일 ${song.files.length}개 · 차트 ${song.charts.length}개 · sha256 ${song.sha256.slice(0, 16)}…`;
    document.getElementById("song-zip").href = `/api/admin/songs/${song.id}/zip`;
    for (const cat of ["play", "pre", "chart"]) {
        const n = song.files.filter((f) => fileCategories(f).includes(cat)).length;
        fileFilter.querySelector(`[data-count="${cat}"]`).textContent = `(${n})`;
    }
    document.getElementById("song-tree").replaceChildren();
    renderTree();
    songDetail.classList.remove("d-none");
    songDetail.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

document.getElementById("song-delete").addEventListener("click", async () => {
    const song = currentSong;
    if (!song) return;
    const reason = prompt(`#${song.id} ${song.folder} 곡을 사용 중지하는 사유를 적으세요(사용 중지 목록에 남습니다).`);
    if (reason === null) return;
    const typed = prompt(
        `#${song.id} ${song.folder} 곡을 사용 중지합니다. 곡 파일은 서버의 var/unused로 옮기고, 번호 ${song.id}는 결번으로 남아 나중에 복원할 수 있습니다.\n` +
        `사용자 클라이언트에서는 다음 동기화 때 목록에서 사라집니다.\n\n` +
        `사용 중지하려면 곡 번호 ${song.id}를 입력하세요.`
    );
    if (typed === null) return;
    if (typed.trim() !== String(song.id)) { alert("곡 번호가 맞지 않아 사용 중지하지 않았습니다."); return; }
    const btn = document.getElementById("song-delete");
    btn.disabled = true;
    try {
        const res = await fetch(`/api/admin/songs/${song.id}`, {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reason }),
        });
        if (!res.ok) { alert(await detail(res)); return; }
        songDetail.classList.add("d-none");
        currentSong = null;
        loadSongs(songPage);
        deletedLoaded = false;
    } finally {
        btn.disabled = false;
    }
});

loadSongs(1);

// ---- 곡 임포트 ----
const MB = 1024 * 1024;
const MIN_CHUNK = MB / 4;
let chunkSize = 32 * MB;  // 프록시가 413을 주면 줄입니다.
let cancelled = false;
let currentUpload = null;

const form = document.getElementById("upload");
const scan = document.getElementById("scan");
const cancel = document.getElementById("cancel");
const state = document.getElementById("upload-state");
const label = document.getElementById("upload-label");
const bar = state.querySelector(".progress-bar");

function gb(n) { return (n / 1024 ** 3).toFixed(2) + "GB"; }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function detail(res) {
    const text = await res.text().catch(() => "");
    try { return JSON.parse(text).detail || text; } catch { return text || `HTTP ${res.status}`; }
}

async function postJson(url, body) {
    const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await detail(res));
    return res.json();
}

function progress(file, sent, index, count) {
    const pct = file.size ? Math.floor((sent / file.size) * 100) : 100;
    bar.style.width = `${pct}%`;
    bar.textContent = `${pct}%`;
    label.textContent = `${index + 1}/${count} ${file.name}: ${gb(sent)} / ${gb(file.size)}`;
}

async function uploadFile(file, index, count) {
    const upload = await postJson("/api/admin/import/uploads", { filename: file.name, size: file.size });
    currentUpload = upload.id;
    let offset = 0;
    let failures = 0;
    while (offset < file.size) {
        if (cancelled) throw new Error("취소했습니다.");
        const end = Math.min(offset + chunkSize, file.size);
        let res;
        try {
            res = await fetch(`/api/admin/import/uploads/${upload.id}?offset=${offset}`, {
                method: "PUT",
                headers: { "Content-Type": "application/octet-stream" },
                body: file.slice(offset, end),
            });
        } catch (e) {
            // 연결이 끊기면 잠시 뒤 받은 곳부터 다시 보냅니다. 큰 요청을 프록시가 끊는 경우도 있어 조각을 줄입니다.
            chunkSize = Math.max(MIN_CHUNK, chunkSize / 2);
            if (++failures > 8) throw new Error("연결이 계속 끊깁니다. 다시 시도하세요.");
            await sleep(1000 * Math.min(30, 2 ** failures));
            continue;
        }
        if (res.status === 413 && chunkSize > MIN_CHUNK) {
            chunkSize = Math.max(MIN_CHUNK, chunkSize / 2);
            continue;
        }
        if (res.status === 409) {
            // 서버가 받은 크기에 맞춰 이어서 보냅니다.
            const m = /offset must be (\d+)/.exec(await detail(res));
            if (!m) throw new Error("업로드 위치가 맞지 않습니다.");
            offset = Number(m[1]);
            continue;
        }
        if (!res.ok) {
            if (res.status >= 500 && ++failures <= 8) {
                await sleep(1000 * Math.min(30, 2 ** failures));
                continue;
            }
            throw new Error(res.status === 413 ? "프록시가 업로드를 막습니다. 요청 크기 제한을 1MB 이상으로 올리세요." : await detail(res));
        }
        failures = 0;
        offset = (await res.json()).received;
        progress(file, offset, index, count);
    }
    currentUpload = null;
    return upload.id;
}

// rar 분할 압축의 볼륨(이름.partN.rar, 또는 이름.rar·이름.rNN)은 묶음별로 모아 함께 올립니다(서버 rar.py와 같은 규칙).
function rarKey(name) {
    let m = /^(.*)\.part(\d+)\.rar$/i.exec(name);
    if (m) return "part:" + m[1].toLowerCase();
    m = /^(.*)\.(rar|r\d\d)$/i.exec(name);
    return m ? "old:" + m[1].toLowerCase() : null;
}

function uploadGroups(files) {
    const groups = new Map();
    for (const file of files) {
        const key = rarKey(file.name) ?? Symbol();
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(file);
    }
    return [...groups.values()];
}

async function uploadGroup(group, start, count) {
    const ids = [];
    try {
        for (const [i, file] of group.entries()) {
            progress(file, 0, start + i, count);
            ids.push(await uploadFile(file, start + i, count));
        }
        return await (ids.length === 1
            ? postJson(`/api/admin/import/uploads/${ids[0]}/finish`)
            : postJson("/api/admin/import/uploads/finish", { ids }));
    } catch (err) {
        for (const id of [...ids, currentUpload].filter(Boolean)) {
            fetch(`/api/admin/import/uploads/${id}`, { method: "DELETE" });
        }
        currentUpload = null;
        throw err;
    }
}

function busy(on) {
    form.querySelectorAll("input, button[type=submit]").forEach((el) => (el.disabled = on));
    scan.disabled = on;
    cancel.classList.toggle("d-none", !on);
    state.classList.toggle("d-none", !on);
}

form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const files = [...document.getElementById("files").files];
    if (!files.length) return;
    cancelled = false;
    busy(true);
    const errors = [];
    let start = 0;
    for (const group of uploadGroups(files)) {
        try {
            await uploadGroup(group, start, files.length);
            refreshJobs();
        } catch (err) {
            errors.push(`${group.map((f) => f.name).join(", ")}: ${err.message}`);
            if (cancelled) break;
        }
        start += group.length;
    }
    busy(false);
    form.reset();
    refreshJobs();
    if (errors.length) alert("업로드하지 못한 파일이 있습니다.\n" + errors.join("\n"));
});

cancel.addEventListener("click", () => { cancelled = true; });

scan.addEventListener("click", async () => {
    scan.disabled = true;
    try {
        await postJson("/api/admin/import/tmp");
    } catch (err) {
        alert(err.message);
    } finally {
        scan.disabled = false;
        refreshJobs();
    }
});

async function setSpeed(job, slow, button) {
    button.disabled = true;
    try {
        const res = await fetch(`/api/admin/import/jobs/${job.id}/speed`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ slow }),
        });
        if (!res.ok) throw new Error(await detail(res));
    } catch (err) {
        alert(err.message);
    } finally {
        refreshJobs();
    }
}

function songLine(song) {
    const li = document.createElement("li");
    const name = document.createElement("code");
    name.textContent = song.folder;
    li.append(name, " ");
    const badge = document.createElement("span");
    if (song.error) {
        badge.className = "badge text-bg-danger";
        badge.textContent = "실패";
        li.append(badge, " " + song.error);
    } else if (song.staged) {
        badge.className = "badge text-bg-info";
        badge.textContent = "검토 대기";
        li.append(badge, ` 차트 ${song.charts}개`);
    } else if (song.skipped === "admin") {
        badge.className = "badge text-bg-secondary";
        badge.textContent = "제외";
        li.append(badge, " 검토에서 제외해 넣지 않았습니다.");
    } else if (song.skipped === "unused") {
        badge.className = "badge text-bg-warning";
        badge.textContent = `사용 중지 곡 #${song.song_id}`;
        li.append(badge, " 사용 중지한 곡이라 가져오지 않았습니다. 다시 쓰려면 사용 중지 목록에서 복원하세요.");
    } else {
        badge.className = song.new_song ? "badge text-bg-success" : "badge text-bg-secondary";
        badge.textContent = song.new_song ? `새 곡 #${song.song_id}` : `기존 곡 #${song.song_id}`;
        li.append(badge, ` 차트 ${song.charts}개 중 새 차트 ${song.new_charts}개`);
        if (song.added_files) li.append(`, 기존 곡에 파일 ${song.added_files.length}개 추가`);
        if (song.conflicts) {
            const warn = document.createElement("span");
            warn.className = "text-warning-emphasis";
            warn.textContent = ` · 내용이 달라 넣지 않은 파일(기존 유지, 폴더는 남김): ${song.conflicts.join(", ")}`;
            li.append(warn);
        }
    }
    return li;
}

// 곡 결과의 수준: failed, skipped, conflict, new, added, same
function songLevel(song) {
    if (song.error) return "failed";
    if (song.staged) return "staged";
    if (song.skipped) return "skipped";
    if (song.conflicts && song.conflicts.length) return "conflict";
    if (song.new_song) return "new";
    if (song.new_charts || (song.added_files && song.added_files.length)) return "added";
    return "same";
}

const filterLevel = document.getElementById("filter-level");
const filterText = document.getElementById("filter-text");

function filtering() { return Boolean(filterLevel.value || filterText.value.trim()); }

function matches(song) {
    if (filterLevel.value && songLevel(song) !== filterLevel.value) return false;
    const q = filterText.value.trim().toLowerCase();
    if (!q) return true;
    const text = [song.folder, song.error, song.song_id, ...(song.conflicts || []), ...(song.added_files || [])].join("\n");
    return text.toLowerCase().includes(q);
}

function songList(songs, summary, open) {
    const details = document.createElement("details");
    details.open = open;
    const s = document.createElement("summary");
    s.className = "small";
    s.textContent = summary;
    const ul = document.createElement("ul");
    ul.className = "small mb-0";
    songs.forEach((song) => ul.append(songLine(song)));
    details.append(s, ul);
    return details;
}

function jobCard(job) {
    const card = document.createElement("div");
    card.className = "card mb-2";
    const body = document.createElement("div");
    body.className = "card-body py-2";
    const title = document.createElement("div");
    const name = document.createElement("strong");
    const commitJob = job.kind === "commit";
    name.textContent = (commitJob ? "commit · " : "") + (job.kind === "tmp" ? "var/tmp" : job.name);
    const badge = document.createElement("span");
    badge.className = "badge ms-2 " + ({ running: "text-bg-primary", done: "text-bg-success", failed: "text-bg-danger" })[job.status];
    badge.textContent = ({ running: commitJob ? "등록 중" : "푸는 중", done: "완료", failed: "실패" })[job.status];
    title.append(name, badge);
    if (!commitJob && job.stage_id && stageIds.has(job.stage_id)) {
        const review = document.createElement("button");
        review.type = "button";
        review.className = "btn btn-sm btn-outline-primary ms-2 py-0";
        review.textContent = "검토하기";
        review.addEventListener("click", () => openStage(job.stage_id));
        title.append(review);
    }
    if (job.slow) {
        const slow = document.createElement("span");
        slow.className = "badge ms-1 text-bg-secondary";
        slow.textContent = job.status === "running" && job.resting ? "천천히 · 쉬는 중" : "천천히";
        title.append(slow);
    }
    if (job.status === "running") {
        const speed = document.createElement("button");
        speed.type = "button";
        speed.className = "btn btn-sm btn-outline-secondary ms-2 py-0";
        speed.textContent = job.slow ? "보통 속도로" : "천천히 진행";
        speed.addEventListener("click", () => setSpeed(job, !job.slow, speed));
        title.append(speed);
    }
    body.append(title);

    const failed = job.songs.filter((s) => s.error);
    const skipped = job.songs.filter((s) => songLevel(s) === "skipped");
    const ok = job.songs.length - failed.length - skipped.length;
    const conflicts = job.songs.filter((s) => songLevel(s) === "conflict").length;
    const line = document.createElement("div");
    line.className = "small";
    if (job.total === null) line.textContent = "곡을 찾는 중…";
    else if (commitJob) line.textContent = `${job.done}건 처리(곡 ${job.total}개) · 성공 ${ok} · 충돌 ${conflicts} · 건너뜀 ${skipped.length} · 실패 ${failed.length}`;
    else line.textContent = `${job.done} / ${job.total}곡 · 검토 대기에 넣음 ${ok} · 실패 ${failed.length}`;
    body.append(line);
    if (job.status === "running" && job.total) {
        const p = document.createElement("div");
        p.className = "progress my-1";
        p.style.height = "6px";
        const b = document.createElement("div");
        b.className = "progress-bar";
        b.style.width = `${Math.floor((job.done / job.total) * 100)}%`;
        p.append(b);
        body.append(p);
    }
    if (job.error) {
        const err = document.createElement("div");
        err.className = "text-danger small";
        err.textContent = job.error;
        body.append(err);
    }
    if (filtering()) {
        const hits = job.songs.filter(matches);
        if (hits.length) body.append(songList(hits, `필터에 맞는 ${hits.length}곡`, true));
        else body.append(Object.assign(document.createElement("div"), { className: "small text-muted", textContent: "필터에 맞는 곡이 없습니다." }));
    } else {
        if (failed.length) body.append(songList(failed, `실패 ${failed.length}곡`, true));
        if (skipped.length) body.append(songList(skipped, `사용 중지로 건너뜀 ${skipped.length}곡`, false));
        if (ok) body.append(songList(job.songs.filter((s) => !s.error && !s.skipped), `${commitJob ? "성공" : "검토 대기에 넣은"} ${ok}곡`, false));
    }
    card.append(body);
    return card;
}

let lastJobs = [];
let timer = null;
async function refreshJobs() {
    clearTimeout(timer);
    let jobs;
    try {
        const res = await fetch("/api/admin/import/jobs");
        if (!res.ok) return;
        jobs = await res.json();
    } catch {
        timer = setTimeout(refreshJobs, 5000);
        return;
    }
    // 작업이 끝나면 검토 대기 목록과 열어 둔 묶음을 새로 읽습니다.
    const finished = jobs.some((j) => j.status !== "running" && lastJobs.some((o) => o.id === j.id && o.status === "running"));
    lastJobs = jobs;
    renderJobs();
    if (finished || !stagesLoaded) {
        loadStages();
        if (currentStage && finished) loadStage(stagePage);
    }
    if (jobs.some((j) => j.status === "running")) timer = setTimeout(refreshJobs, 2000);
}

function renderJobs() {
    const jobs = lastJobs;
    const box = document.getElementById("jobs");
    // 펼쳐 둔 목록은 새로 그려도 유지합니다.
    const opened = new Set([...box.querySelectorAll("details[open]")].map((d) => d.dataset.key));
    box.replaceChildren(...jobs.map(jobCard));
    box.querySelectorAll(".card").forEach((card, i) => {
        card.querySelectorAll("details").forEach((d, j) => {
            d.dataset.key = `${jobs[i].id}:${j}`;
            if (opened.has(d.dataset.key)) d.open = true;
        });
    });
    if (!jobs.length) box.innerHTML = '<p class="text-muted small">작업이 없습니다.</p>';
}

// 필터는 받은 목록을 다시 그리기만 합니다(서버에 다시 묻지 않음).
filterLevel.addEventListener("change", renderJobs);
filterText.addEventListener("input", renderJobs);

// ---- 검토 대기(staged) ----
let stagesLoaded = false;
let stageIds = new Set();  // 남아 있는 묶음(작업의 "검토하기" 버튼)
let currentStage = null;  // 열어 둔 묶음(한 쪽 곡 포함)
let stagePage = 1;
const stageCard = document.getElementById("stage");
const stageShow = document.getElementById("stage-show");
const stageQ = document.getElementById("stage-q");
const stageSort = document.getElementById("stage-sort");
const FIELD = { title: "이름", artist: "작곡가", bpm: "BPM", genre: "장르" };
const SUGGESTION = {
    new: ["새 곡", "text-bg-success"],
    add: ["기존 곡에 추가", "text-bg-secondary"],
    ambiguous: ["여러 곡과 겹침", "text-bg-warning"],
    unused: ["사용 중지 곡과 겹침", "text-bg-danger"],
    similar: ["비슷한 곡 있음", "text-bg-info"],
};

function stageCounts(stage) {
    const c = stage.counts;
    return `곡 ${stage.units}개 · 채보 ${stage.charts}개 · ${size(stage.size)} · 새 곡 ${c.new} · 추가 ${c.add} · 여러 곡과 겹침 ${c.ambiguous} · 사용 중지 곡 ${c.unused} · 비슷한 곡 ${c.similar} · 대상 바꿈 ${c.changed} · 제외 ${c.skip}` +
        (c.failed ? ` · 지난 commit 실패 ${c.failed}` : "");
}

async function loadStages() {
    stagesLoaded = true;
    const res = await fetch("/api/admin/import/stages");
    if (!res.ok) return;
    const stages = await res.json();
    stageIds = new Set(stages.map((s) => s.id));
    renderJobs();
    const box = document.getElementById("stages");
    if (!stages.length) {
        box.innerHTML = '<p class="text-muted small">검토할 묶음이 없습니다.</p>';
    } else {
        box.replaceChildren(...stages.map((stage) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "list-group-item list-group-item-action small" + (currentStage?.id === stage.id ? " active" : "");
            const title = document.createElement("strong");
            title.textContent = stage.name;
            const status = document.createElement("span");
            status.className = "badge ms-2 " + (stage.status === "ready" ? "text-bg-info" : "text-bg-primary");
            status.textContent = ({ ready: "검토 대기", staging: "푸는 중", committing: "등록 중" })[stage.status];
            const meta = document.createElement("div");
            meta.textContent = `${new Date(stage.created_at * 1000).toLocaleString()} · ${stageCounts(stage)}`;
            btn.append(title, status, meta);
            btn.addEventListener("click", () => openStage(stage.id));
            return btn;
        }));
        box.classList.add("list-group");
    }
    if (currentStage && !stages.some((s) => s.id === currentStage.id)) {
        currentStage = null;
        stageCard.classList.add("d-none");
    }
}

function openStage(id) {
    currentStage = { id };
    stageShow.value = "";
    stageSort.value = "";
    stageQ.value = "";
    loadStage(1).then(() => stageCard.scrollIntoView({ behavior: "smooth", block: "nearest" }));
    loadStages();
}

async function loadStage(page = 1) {
    if (!currentStage) return;
    const params = new URLSearchParams({ page, show: stageShow.value, q: stageQ.value.trim(), sort: stageSort.value });
    const res = await fetch(`/api/admin/import/stages/${currentStage.id}?${params}`);
    if (res.status === 404) { currentStage = null; stageCard.classList.add("d-none"); loadStages(); return; }
    if (!res.ok) { alert(await detail(res)); return; }
    const stage = await res.json();
    currentStage = stage;
    stagePage = stage.page;
    document.getElementById("stage-title").textContent = stage.name;
    const analyzed = stage.analyzed_at ? ` · 판정 ${new Date(stage.analyzed_at * 1000).toLocaleString()}` : "";
    document.getElementById("stage-meta").textContent = stageCounts(stage) + analyzed;
    document.getElementById("stage-error").textContent = stage.error || (stage.status !== "ready" ? "작업 중이라 바꿀 수 없습니다. 끝나면 새로 읽습니다." : "");
    const locked = stage.status !== "ready";
    for (const id of ["stage-analyze", "stage-commit", "stage-discard"]) document.getElementById(id).disabled = locked;
    const rows = document.getElementById("unit-rows");
    rows.replaceChildren(...stage.items.flatMap((unit) => unitRows(stage, unit, locked)));
    document.getElementById("unit-empty").classList.toggle("d-none", stage.items.length > 0);
    renderPager(document.getElementById("unit-pager"), stage, loadStage);
    stageCard.classList.remove("d-none");
    if (locked) { clearTimeout(timer); timer = setTimeout(refreshJobs, 2000); }
}

function songName(stage, id) {
    const folder = stage.songs[String(id)];
    return folder === undefined ? `#${id}` : `#${id} ${folder}`;
}

// 기존 곡 고르기: 폴더명·채보 파일명·번호로 검색하는 작은 목록. 검색어가 없으면 판정에 나온 곡(suggested)을 먼저 보여줍니다.
function songPicker(stage, suggested, onPick) {
    const box = document.createElement("div");
    box.className = "w-100";
    const input = document.createElement("input");
    input.type = "search";
    input.className = "form-control form-control-sm";
    input.placeholder = "곡 검색(폴더명, 채보 파일명, 번호)";
    input.setAttribute("aria-label", "넣을 곡 검색");
    const list = document.createElement("div");
    list.className = "list-group list-group-flush border rounded mt-1 overflow-auto";
    list.style.maxHeight = "12rem";
    const fill = (songs, empty) => {
        list.replaceChildren();
        for (const song of songs) {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "list-group-item list-group-item-action py-1 small text-break";
            btn.textContent = `#${song.id} ${song.folder || "(이름 없음)"}` + (song.charts !== undefined ? ` · 차트 ${song.charts}개` : "");
            btn.addEventListener("click", () => onPick(song.id));
            list.append(btn);
        }
        if (!songs.length) list.append(Object.assign(document.createElement("div"), { className: "list-group-item py-1 small text-muted", textContent: empty }));
    };
    const showSuggested = () => fill(
        suggested.map((id) => ({ id, folder: stage.songs[String(id)] })),
        "검색어를 입력하세요.",
    );
    let timer = null;
    let seq = 0;
    input.addEventListener("input", () => {
        clearTimeout(timer);
        const q = input.value.trim();
        if (!q) { showSuggested(); return; }
        timer = setTimeout(async () => {
            const mine = ++seq;
            const res = await fetch(`/api/admin/songs?${new URLSearchParams({ q, page: 1 })}`);
            if (mine !== seq) return;  // 더 새 검색이 있으면 버립니다.
            if (!res.ok) { fill([], "검색하지 못했습니다."); return; }
            const data = await res.json();
            fill(data.items, "찾는 곡이 없습니다.");
        }, 250);
    });
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); list.querySelector("button")?.click(); }
        if (e.key === "Escape") box.remove();
    });
    showSuggested();
    box.append(input, list);
    setTimeout(() => input.focus(), 0);
    return box;
}

// 대상 고르기: auto/new/skip, 곡 번호. inherit면 "곡 설정 따름"(null)도 있습니다. suggested는 고를 때 먼저 보일 곡 번호.
function targetControl(stage, value, onChange, { inherit = false, locked = false, suggested = [] } = {}) {
    const wrap = document.createElement("div");
    wrap.className = "d-flex gap-1 align-items-center flex-wrap";
    const select = document.createElement("select");
    select.className = "form-select form-select-sm w-auto";
    select.disabled = locked;
    const options = [...(inherit ? [["inherit", "곡 설정 따름"]] : []), ["auto", "자동(판정대로)"], ["new", "새 곡"], ["song", "기존 곡에 추가…"], ["skip", "제외"]];
    for (const [v, text] of options) select.append(new Option(text, v));
    const kind = value === null ? "inherit" : typeof value === "number" ? "song" : value;
    select.value = kind;
    const name = document.createElement("span");
    name.className = "text-muted text-break";
    if (typeof value === "number") name.textContent = songName(stage, value);
    const change = document.createElement("button");
    change.type = "button";
    change.className = "btn btn-link btn-sm p-0" + (kind === "song" ? "" : " d-none");
    change.textContent = "다른 곡 고르기";
    change.disabled = locked;
    let picker = null;
    const openPicker = () => {
        if (picker?.isConnected) { picker.querySelector("input").focus(); return; }
        picker = songPicker(stage, suggested, (id) => { picker.remove(); onChange(id); });
        wrap.append(picker);
    };
    change.addEventListener("click", openPicker);
    select.addEventListener("change", () => {
        if (select.value === "song") { openPicker(); return; }
        picker?.remove();
        onChange(select.value === "inherit" ? null : select.value);
    });
    wrap.append(select, name, change);
    return wrap;
}

function suggestionCell(stage, unit) {
    const td = document.createElement("td");
    const s = unit.suggestion;
    if (!s) { td.textContent = "-"; return td; }
    const [text, cls] = SUGGESTION[s.kind];
    const badge = document.createElement("span");
    badge.className = "badge " + cls;
    badge.textContent = text;
    td.append(badge);
    const ids = s.kind === "unused" ? [s.deleted] : s.songs;
    if (ids.length) {
        const div = document.createElement("div");
        div.className = "text-muted text-break";
        div.textContent = s.kind === "unused" ? `사용 중지 #${s.deleted}`
            : s.kind === "similar" ? ids.map((id) => `${songName(stage, id)} (${s.matches[String(id)].map((f) => FIELD[f]).join("·")})`).join(", ")
            : ids.map((id) => songName(stage, id)).join(", ");
        td.append(div);
    }
    if (s.kind === "similar") {
        // 겹치는 채보는 없고 이름·작곡가·BPM·장르 중 2개 이상 같은 곡만 있을 때 "자동"이 하는 일
        const div = document.createElement("div");
        div.className = "small";
        div.textContent = s.merge ? `자동: ${songName(stage, s.merge)}에 추가 (가장 많이 겹침)` : "자동: 새 곡 (가장 많이 겹치는 곡이 여럿)";
        td.append(div);
    }
    return td;
}

function chartStatus(stage, chart) {
    if (chart.deleted) return `사용 중지 #${chart.deleted}의 채보`;
    if (chart.song_id) return `${songName(stage, chart.song_id)}에 있음`;
    return "새 채보";
}

async function saveUnit(unit, target, charts) {
    const res = await fetch(`/api/admin/import/stages/${currentStage.id}/units/${unit.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target, charts }),
    });
    if (!res.ok) alert(await detail(res));
    await loadStage(stagePage);
    loadStages();
}

function unitRows(stage, unit, locked) {
    const tr = document.createElement("tr");
    const folder = document.createElement("td");
    folder.className = "text-break";
    const code = document.createElement("code");
    code.textContent = unit.folder;
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "btn btn-link btn-sm p-0 ms-1 align-baseline";
    const chartChanged = unit.charts.some((c) => c.target !== null);
    toggle.textContent = `채보 ${unit.charts.length}개${chartChanged ? " · 채보별 지정" : ""} · 파일 보기 ▾`;
    folder.append(code, toggle);
    if (unit.result) {
        const warn = document.createElement("div");
        warn.className = unit.result.error ? "text-danger" : "text-warning-emphasis";
        const parts = [];
        if (unit.result.error) parts.push(`지난 commit 실패: ${unit.result.error}`);
        if (unit.result.conflicts.length) parts.push(`내용이 달라 넣지 않은 파일(기존 유지): ${unit.result.conflicts.join(", ")}`);
        warn.textContent = parts.join(" · ");
        folder.append(warn);
    }
    const target = document.createElement("td");
    const suggested = unit.suggestion?.songs || [];
    if (unit.suggestion?.merge && !suggested.includes(unit.suggestion.merge)) suggested.unshift(unit.suggestion.merge);
    target.append(targetControl(stage, unit.target, (v) => saveUnit(unit, v, null), { locked, suggested }));
    const sz = document.createElement("td");
    sz.className = "text-end text-nowrap";
    sz.textContent = `${size(unit.size)} · 파일 ${unit.files}개`;
    tr.append(folder, suggestionCell(stage, unit), target, sz);

    const sub = document.createElement("tr");
    sub.className = chartChanged ? "" : "d-none";
    const td = document.createElement("td");
    td.colSpan = 4;
    const ul = document.createElement("ul");
    ul.className = "list-unstyled ms-3 mb-0";
    for (const chart of unit.charts) {
        const li = document.createElement("li");
        li.className = "d-flex gap-2 align-items-center flex-wrap py-1";
        const name = document.createElement("code");
        name.textContent = chart.path;
        const st = document.createElement("span");
        st.className = "text-muted";
        st.textContent = chartStatus(stage, chart);
        const meta = document.createElement("span");
        meta.className = "small text-break";
        meta.textContent = [chart.title, chart.artist, chart.bpm && `BPM ${chart.bpm}`, chart.genre].filter(Boolean).join(" / ");
        li.append(name, meta, st, targetControl(stage, chart.target, (v) => saveUnit(unit, unit.target, { [chart.path]: v }), { inherit: true, locked, suggested: chart.song_id ? [chart.song_id] : suggested }));
        ul.append(li);
    }
    td.append(ul);
    // 채보 외 파일(키음·BGA·이미지 등). 펼칠 때 한 번 읽습니다.
    const others = unit.files - unit.charts.length;
    const files = document.createElement("details");
    files.className = "ms-3 mt-1 small";
    const fsum = document.createElement("summary");
    fsum.textContent = `채보 외 파일 ${others}개`;
    const flist = document.createElement("ul");
    flist.className = "list-unstyled ms-3 mb-0 overflow-auto";
    flist.style.maxHeight = "16rem";
    files.append(fsum, flist);
    files.addEventListener("toggle", async () => {
        if (!files.open || files.dataset.loaded) return;
        files.dataset.loaded = "1";
        flist.textContent = "읽는 중…";
        const res = await fetch(`/api/admin/import/stages/${stage.id}/units/${unit.id}/files`);
        if (!res.ok) { flist.textContent = await detail(res); delete files.dataset.loaded; return; }
        const items = await res.json();
        if (!items.length) { flist.textContent = "없음"; return; }
        flist.replaceChildren(...items.map((f) => {
            const li = document.createElement("li");
            const code = document.createElement("code");
            code.textContent = f.path;
            li.append(code, ` ${size(f.size)}`);
            return li;
        }));
    });
    if (others > 0) td.append(files);
    sub.append(td);
    toggle.addEventListener("click", () => sub.classList.toggle("d-none"));
    return [tr, sub];
}

stageShow.addEventListener("change", () => loadStage(1));
stageSort.addEventListener("change", () => loadStage(1));
let stageQTimer = null;
stageQ.addEventListener("input", () => { clearTimeout(stageQTimer); stageQTimer = setTimeout(() => loadStage(1), 300); });

document.getElementById("stage-analyze").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
        await postJson(`/api/admin/import/stages/${currentStage.id}/analyze`);
    } catch (err) {
        alert(err.message);
    }
    await loadStage(stagePage);
    loadStages();
});

document.getElementById("stage-commit").addEventListener("click", async () => {
    const stage = currentStage;
    const c = stage.counts;
    let msg = `${stage.name}의 곡 ${stage.units}개를 지정한 대로 등록합니다(제외 ${c.skip}개).`;
    if (c.ambiguous) msg += `\n여러 곡과 겹치는 곡 ${c.ambiguous}개 중 자동으로 둔 곡은 실패로 남습니다.`;
    if (!confirm(msg)) return;
    try {
        await postJson(`/api/admin/import/stages/${stage.id}/commit?slow=${document.getElementById("slow-commit").checked}`);
    } catch (err) {
        alert(err.message);
    }
    await loadStage(stagePage);
    loadStages();
    refreshJobs();
});

document.getElementById("stage-discard").addEventListener("click", async () => {
    const stage = currentStage;
    if (!confirm(`${stage.name}의 곡 ${stage.units}개를 등록하지 않고 버립니다. 풀어 둔 파일은 지워집니다.`)) return;
    const res = await fetch(`/api/admin/import/stages/${stage.id}`, { method: "DELETE" });
    if (!res.ok) { alert(await detail(res)); return; }
    currentStage = null;
    stageCard.classList.add("d-none");
    loadStages();
});

refreshJobs();
