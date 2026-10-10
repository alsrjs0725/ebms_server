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
    const line = document.createElement("div");
    line.className = "small";
    line.textContent = job.total === null ? "곡을 찾는 중…" : `${job.done} / ${job.total}곡 처리 · 성공 ${ok} · 실패 ${failed.length}`;
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
    if (failed.length) body.append(songList(failed, `실패 ${failed.length}곡`, true));
    if (ok) body.append(songList(job.songs.filter((s) => !s.error), `성공 ${ok}곡`, false));
    card.append(body);
    return card;
}

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
    if (jobs.some((j) => j.status === "running")) timer = setTimeout(refreshJobs, 2000);
}
refreshJobs();
