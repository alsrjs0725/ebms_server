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
const SONG_LIMIT = Number(document.getElementById("tab-songs").dataset.limit);
const songRows = document.getElementById("song-rows");
const songMore = document.getElementById("song-more");
const songDetail = document.getElementById("song-detail");
let songQuery = "";
let lastSongId = null;
let currentSong = null;

function size(n) {
    if (n < 1024) return `${n}B`;
    if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)}KB`;
    if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)}MB`;
    return `${(n / 1024 ** 3).toFixed(2)}GB`;
}

async function loadSongs(more) {
    const params = new URLSearchParams({ q: songQuery });
    if (more && lastSongId !== null) params.set("before", lastSongId);
    const res = await fetch(`/api/admin/songs?${params}`);
    if (!res.ok) { alert(await detail(res)); return; }
    const songs = await res.json();
    if (!more) songRows.replaceChildren();
    for (const song of songs) {
        const tr = document.createElement("tr");
        tr.style.cursor = "pointer";
        tr.dataset.id = song.id;
        tr.tabIndex = 0;
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
    if (songs.length) lastSongId = songs[songs.length - 1].id;
    songMore.classList.toggle("d-none", songs.length < SONG_LIMIT);
    document.getElementById("song-empty").classList.toggle("d-none", songRows.children.length > 0);
}

document.getElementById("song-search").addEventListener("submit", (e) => {
    e.preventDefault();
    songQuery = document.getElementById("song-q").value.trim();
    lastSongId = null;
    loadSongs(false);

// ---- 사용 중지한 곡 ----
const deletedRows = document.getElementById("deleted-rows");
const deletedMore = document.getElementById("deleted-more");
let deletedQuery = "";
let lastDeletedId = null;
let deletedLoaded = false;

async function loadDeleted(more) {
    const params = new URLSearchParams({ q: deletedQuery });
    if (more && lastDeletedId !== null) params.set("before", lastDeletedId);
    const res = await fetch(`/api/admin/songs/deleted?${params}`);
    if (!res.ok) { alert(await detail(res)); return; }
    const rows = await res.json();
    if (!more) deletedRows.replaceChildren();
    for (const row of rows) {
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
        btn.addEventListener("click", () => restoreSong(row, tr, btn));
        td.append(btn);
        tr.append(td);
        deletedRows.append(tr);
    }
    if (rows.length) lastDeletedId = rows[rows.length - 1].song_id;
    deletedMore.classList.toggle("d-none", rows.length < SONG_LIMIT);
    document.getElementById("deleted-empty").classList.toggle("d-none", deletedRows.children.length > 0);
    deletedLoaded = true;
}

async function restoreSong(row, tr, btn) {
    if (!confirm(`#${row.song_id} ${row.folder} 곡을 같은 번호로 복원합니다.`)) return;
    btn.disabled = true;
    try {
        const res = await fetch(`/api/admin/songs/${row.song_id}/restore`, { method: "POST" });
        if (!res.ok) { alert(await detail(res)); return; }
        tr.remove();
        document.getElementById("deleted-empty").classList.toggle("d-none", deletedRows.children.length > 0);
        // 곡 목록은 다음 검색 때 다시 보입니다.
    } finally {
        btn.disabled = false;
    }
}

document.getElementById("deleted-search").addEventListener("submit", (e) => {
    e.preventDefault();
    deletedQuery = document.getElementById("deleted-q").value.trim();
    lastDeletedId = null;
    loadDeleted(false);
});
deletedMore.addEventListener("click", () => loadDeleted(true));
// 탭을 처음 열 때, 그리고 곡을 사용 중지한 뒤 다시 열 때 새로 읽습니다.
document.querySelector('[data-tab="deleted"]').addEventListener("click", () => { if (!deletedLoaded) loadDeleted(false); });
});
songMore.addEventListener("click", () => loadSongs(true));

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

function renderNode(node, songId) {
    const ul = document.createElement("ul");
    ul.className = "list-unstyled ms-3 mb-0";
    const byName = (a, b) => a.localeCompare(b);
    for (const name of [...node.dirs.keys()].sort(byName)) {
        const dir = node.dirs.get(name);
        const [total, count] = nodeSize(dir);
        const li = document.createElement("li");
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = `📁 ${name}/ `;
        const meta = document.createElement("span");
        meta.className = "text-muted";
        meta.textContent = `(${count}개, ${size(total)})`;
        summary.append(meta);
        details.append(summary, renderNode(dir, songId));
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
    const tree = renderNode(fileTree(song.files), song.id);
    tree.classList.remove("ms-3");
    // 최상위 폴더 하나만 있으면 펼쳐 둡니다.
    const top = tree.querySelectorAll(":scope > li > details");
    if (top.length === 1) top[0].open = true;
    document.getElementById("song-tree").replaceChildren(tree);
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
        songRows.querySelector(`tr[data-id="${song.id}"]`)?.remove();
        document.getElementById("song-empty").classList.toggle("d-none", songRows.children.length > 0);
        songDetail.classList.add("d-none");
        currentSong = null;
        deletedLoaded = false;
    } finally {
        btn.disabled = false;
    }
});

loadSongs(false);

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
    const job = await postJson(`/api/admin/import/uploads/${upload.id}/finish`);
    currentUpload = null;
    return job;
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
    for (const [i, file] of files.entries()) {
        progress(file, 0, i, files.length);
        try {
            await uploadFile(file, i, files.length);
            refreshJobs();
        } catch (err) {
            errors.push(`${file.name}: ${err.message}`);
            if (currentUpload) fetch(`/api/admin/import/uploads/${currentUpload}`, { method: "DELETE" });
            currentUpload = null;
            if (cancelled) break;
        }
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

// 곡 결과의 수준: failed, conflict, new, added, same
function songLevel(song) {
    if (song.error) return "failed";
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
    name.textContent = job.kind === "tmp" ? "var/tmp" : job.name;
    const badge = document.createElement("span");
    badge.className = "badge ms-2 " + ({ running: "text-bg-primary", done: "text-bg-success", failed: "text-bg-danger" })[job.status];
    badge.textContent = ({ running: "등록 중", done: "완료", failed: "실패" })[job.status];
    title.append(name, badge);
    body.append(title);

    const failed = job.songs.filter((s) => s.error);
    const ok = job.songs.length - failed.length;
    const conflicts = job.songs.filter((s) => songLevel(s) === "conflict").length;
    const line = document.createElement("div");
    line.className = "small";
    line.textContent = job.total === null ? "곡을 찾는 중…" : `${job.done} / ${job.total}곡 처리 · 성공 ${ok} · 충돌 ${conflicts} · 실패 ${failed.length}`;
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
        if (ok) body.append(songList(job.songs.filter((s) => !s.error), `성공 ${ok}곡`, false));
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
    lastJobs = jobs;
    renderJobs();
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
refreshJobs();
