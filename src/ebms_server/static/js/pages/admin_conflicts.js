// 곡 임포트 검토의 병합 충돌 해결 창. admin_songs.js 다음에 읽어 그 전역(currentStage, loadStage, detail, size 등)을 씁니다.
// 충돌 파일마다 기존 곡의 파일과 새 파일을 나란히 비교하고 old(기존 유지)·new(새 파일로 바꿈)를 고른 뒤 그 곡만 다시 넣습니다.

const conflictModalEl = document.getElementById("conflict-modal");
const conflictModal = new bootstrap.Modal(conflictModalEl);
const conflictList = document.getElementById("conflict-list");
const conflictView = document.getElementById("conflict-view");
const conflictCount = document.getElementById("conflict-count");
const conflictApply = document.getElementById("conflict-apply");
const conflictAllOld = document.getElementById("conflict-all-old");
const CHART_EXT = /\.(bms|bme|bml|pms|bmson)$/i;
const KIND = { sound: "키음", image: "이미지", video: "영상", other: "파일" };

let conflict = null;  // {stageId, unit, items, songs, index}
let conflictKeys = null;  // 지금 보는 파일의 단축키 처리(영상 프레임 이동 등)

// 곡 행에 붙일 "충돌 해결" 버튼. 고를 충돌 파일이 없으면 null.
function conflictButton(stage, unit) {
    const paths = (unit.result?.conflicts || []).filter((p) => !CHART_EXT.test(p) || p.includes("/"));
    if (!paths.length) return null;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-warning btn-sm py-0 ms-2 align-baseline";
    const chosen = paths.filter((p) => unit.resolutions?.[p]).length;
    btn.textContent = `충돌 해결 (${chosen}/${paths.length})`;
    btn.addEventListener("click", () => openConflicts(stage.id, unit));
    return btn;
}

function conflictBase() {
    return `/api/admin/import/stages/${conflict.stageId}/units/${conflict.unit.id}/conflicts`;
}

function conflictUrl(item, side, extra = "") {
    return `${conflictBase()}/file?path=${encodeURIComponent(item.path)}&side=${side}${extra}`;
}

async function openConflicts(stageId, unit) {
    conflict = { stageId, unit, items: [], songs: {}, index: 0 };
    document.getElementById("conflict-title").textContent = `충돌 해결 · ${unit.folder}`;
    conflictList.textContent = "읽는 중…";
    conflictView.replaceChildren();
    conflictModal.show();
    const res = await fetch(conflictBase());
    if (!res.ok) { conflictList.textContent = await detail(res); return; }
    const data = await res.json();
    conflict.items = data.items;
    conflict.songs = data.songs;
    const first = conflict.items.findIndex((i) => !i.choice);
    renderConflictList();
    showConflict(first < 0 ? 0 : first);
}

function choiceText(choice) {
    return choice === "old" ? "기존 유지" : choice === "new" ? "새 파일" : "고르지 않음";
}

function renderConflictList() {
    conflictList.replaceChildren(...conflict.items.map((item, i) => {
        const a = document.createElement("button");
        a.type = "button";
        a.className = `list-group-item list-group-item-action d-flex justify-content-between align-items-center gap-2${i === conflict.index ? " active" : ""}`;
        const name = document.createElement("span");
        name.className = "text-break";
        name.textContent = item.old_path && item.old_path !== item.path ? `${item.path} ↔ ${item.old_path}` : item.path;
        const badge = document.createElement("span");
        badge.className = `badge ${item.choice === "new" ? "text-bg-primary" : item.choice === "old" ? "text-bg-secondary" : "text-bg-warning"}`;
        badge.textContent = `${KIND[item.kind]} · ${choiceText(item.choice)}`;
        a.append(name, badge);
        a.addEventListener("click", () => showConflict(i));
        return a;
    }));
    const done = conflict.items.filter((i) => i.choice).length;
    conflictCount.textContent = `고름 ${done}/${conflict.items.length}`;
    conflictApply.disabled = !conflict.items.length || done < conflict.items.length;
}

async function choose(item, choice) {
    const res = await fetch(conflictBase(), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ choices: { [item.path]: choice } }),
    });
    if (!res.ok) { alert(await detail(res)); return false; }
    item.choice = choice;
    renderConflictList();
    renderChoice(item);
    return true;
}

function renderChoice(item) {
    const group = conflictView.querySelector("[data-choice]");
    if (!group) return;
    for (const btn of group.querySelectorAll("button")) {
        const on = btn.dataset.value === item.choice;
        btn.classList.toggle("active", on);
        btn.setAttribute("aria-pressed", on ? "true" : "false");
    }
}

function sideHeader(title, path, bytes, url) {
    const div = document.createElement("div");
    div.className = "small mb-1 text-break";
    const strong = document.createElement("strong");
    strong.textContent = title;
    const code = document.createElement("code");
    code.className = "ms-1";
    code.textContent = path;
    const dl = document.createElement("a");
    dl.className = "ms-2";
    dl.href = url;
    dl.textContent = "원본";
    div.append(strong, code, ` ${bytes == null ? "" : size(bytes)}`, dl);
    return div;
}

function el(tag, className, text) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined) e.textContent = text;
    return e;
}

function showConflict(index) {
    if (!conflict.items.length) { conflictView.textContent = "충돌 파일이 없습니다."; return; }
    conflict.index = Math.max(0, Math.min(index, conflict.items.length - 1));
    conflictKeys = null;
    for (const m of conflictView.querySelectorAll("video, audio")) m.pause();
    renderConflictList();
    const item = conflict.items[conflict.index];
    const song = conflict.songs[String(item.song_id)];

    const head = el("div", "d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2");
    head.append(el("div", "small text-muted", `곡 #${item.song_id}${song ? ` ${song}` : ""}에 넣으려던 ${KIND[item.kind]}`));
    const group = el("div", "btn-group btn-group-sm");
    group.dataset.choice = "1";
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "남길 파일");
    for (const [value, text] of [["old", "1 기존 유지"], ["new", "2 새 파일로 바꿈"]]) {
        const btn = el("button", "btn btn-outline-primary", text);
        btn.type = "button";
        btn.dataset.value = value;
        btn.addEventListener("click", () => choose(item, value));
        group.append(btn);
    }
    head.append(group);

    const cols = el("div", "row g-2");
    const left = el("div", "col-6");
    const right = el("div", "col-6");
    left.append(sideHeader("기존", item.old_path || "(없음)", item.old_size, conflictUrl(item, "old", "&raw=true")));
    right.append(sideHeader("새", item.path, item.new_size, conflictUrl(item, "new", "&raw=true")));
    cols.append(left, right);
    const body = el("div", "mt-2");
    conflictView.replaceChildren(head, cols, body);
    renderChoice(item);

    const oldKind = item.old_kind || item.kind;
    if (item.kind === "image" && oldKind === "image") showImages(item, left, right, body);
    else if (item.kind === "sound" && oldKind === "sound") showSounds(item, left, right, body);
    else if (item.kind === "video" && oldKind === "video") showVideos(item, left, right, body);
    else if (item.kind === "other" && oldKind === "other") showText(item, body);
    else {
        // 같은 BGA 자리에 이미지와 영상: 각자 보여 주기만 합니다.
        left.append(mediaElement(oldKind, conflictUrl(item, "old")));
        right.append(mediaElement(item.kind, conflictUrl(item, "new")));
        body.append(el("p", "small text-muted", "이미지와 영상이라 차이를 계산하지 않습니다."));
    }
}

function mediaElement(kind, url) {
    if (kind === "image") { const img = el("img", "conflict-media d-block"); img.src = url; img.alt = ""; return img; }
    if (kind === "sound") { const a = el("audio", "w-100"); a.controls = true; a.preload = "auto"; a.src = url; return a; }
    if (kind === "video") { const v = el("video", "conflict-media d-block w-100"); v.controls = true; v.muted = true; v.preload = "auto"; v.src = url; return v; }
    const a = el("a", "", "내려받기"); a.href = url; return a;
}

// ---- 이미지: 나란히 / 겹쳐 보기(슬라이더) / 차이 ----

function loadImage(url) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = () => reject(new Error("그림을 읽지 못했습니다."));
        img.src = url;
    });
}

async function showImages(item, left, right, body) {
    const status = el("p", "small text-muted", "그림을 읽는 중…");
    body.append(status);
    let a, b;
    try {
        [a, b] = await Promise.all([loadImage(conflictUrl(item, "old")), loadImage(conflictUrl(item, "new"))]);
    } catch (err) {
        status.textContent = err.message;
        return;
    }
    if (conflict.items[conflict.index] !== item) return;
    for (const [col, img] of [[left, a], [right, b]]) {
        const shown = el("img", "conflict-media d-block");
        shown.src = img.src;
        shown.alt = "";
        col.append(shown, el("div", "small text-muted", `${img.naturalWidth}×${img.naturalHeight}`));
    }
    // 새 그림을 기존 그림 크기로 맞춰 비교합니다(크기만 다른 그림도 같은 자리에서 봄).
    const w = a.naturalWidth, h = a.naturalHeight;
    const pixels = (img) => {
        const c = document.createElement("canvas");
        c.width = w; c.height = h;
        const ctx = c.getContext("2d");
        ctx.drawImage(img, 0, 0, w, h);
        return ctx.getImageData(0, 0, w, h);
    };
    const pa = pixels(a), pb = pixels(b);
    const heat = new ImageData(w, h);
    let sum = 0, changed = 0;
    for (let i = 0; i < pa.data.length; i += 4) {
        let m = 0;
        for (let k = 0; k < 4; k++) m = Math.max(m, Math.abs(pa.data[i + k] - pb.data[i + k]));
        sum += m;
        if (m > 0.25 * 255) changed++;
        const g = (pa.data[i] + pa.data[i + 1] + pa.data[i + 2]) / 12;  // 바탕은 흐린 흑백
        heat.data[i] = Math.min(255, g + m * 2);
        heat.data[i + 1] = g;
        heat.data[i + 2] = g;
        heat.data[i + 3] = 255;
    }
    const n = w * h;
    status.textContent = `평균 차이 ${(sum / n / 255 * 100).toFixed(2)}% · 크게(25% 넘게) 다른 픽셀 ${(changed / n * 100).toFixed(2)}%`
        + (a.naturalWidth !== b.naturalWidth || a.naturalHeight !== b.naturalHeight ? " · 크기가 달라 새 그림을 기존 크기로 맞춰 비교" : "");

    const canvas = el("canvas", "conflict-canvas border");
    canvas.width = w; canvas.height = h;
    // 작은 그림은 4배까지 키워 보여 줍니다.
    canvas.style.width = `min(100%, ${w * 4}px)`;
    const ctx = canvas.getContext("2d");
    const range = el("input", "form-range");
    range.type = "range"; range.min = 0; range.max = 100; range.value = 50;
    range.setAttribute("aria-label", "겹쳐 보기 위치");
    const modes = el("div", "btn-group btn-group-sm mb-2");
    let mode = "overlay";
    const draw = () => {
        if (mode === "diff") { ctx.putImageData(heat, 0, 0); return; }
        ctx.putImageData(pa, 0, 0);
        const x = Math.round(w * range.value / 100);
        if (x > 0) ctx.putImageData(pb, 0, 0, 0, 0, x, h);
        ctx.fillStyle = "#dc3545";
        ctx.fillRect(Math.min(x, w - 1), 0, Math.max(1, w / 256), h);
    };
    for (const [value, text] of [["overlay", "겹쳐 보기(왼쪽 새 · 오른쪽 기존)"], ["diff", "차이(빨갈수록 다름)"]]) {
        const btn = el("button", `btn btn-outline-secondary${value === mode ? " active" : ""}`, text);
        btn.type = "button";
        btn.addEventListener("click", () => {
            mode = value;
            for (const x of modes.children) x.classList.toggle("active", x === btn);
            range.classList.toggle("d-none", mode === "diff");
            draw();
        });
        modes.append(btn);
    }
    range.addEventListener("input", draw);
    body.append(modes, canvas, range);
    draw();
}

// ---- 키음: 플레이어 + 파형 + 상관계수 ----

function drawWave(canvas, peaks, seconds, maxSeconds, color) {
    const ratio = window.devicePixelRatio || 1;
    canvas.width = canvas.clientWidth * ratio;
    canvas.height = canvas.clientHeight * ratio;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const width = canvas.width * (maxSeconds ? seconds / maxSeconds : 1);
    const mid = canvas.height / 2;
    ctx.fillStyle = color;
    peaks.forEach((p, i) => {
        const x = (i / peaks.length) * width;
        const hgt = Math.max(1, p * mid);
        ctx.fillRect(x, mid - hgt, Math.max(1, width / peaks.length), hgt * 2);
    });
}

async function showSounds(item, left, right, body) {
    const players = [mediaElement("sound", conflictUrl(item, "old")), mediaElement("sound", conflictUrl(item, "new"))];
    left.append(players[0]);
    right.append(players[1]);
    const status = el("p", "small text-muted", "파형을 계산하는 중…");
    body.append(status);
    const res = await fetch(`${conflictBase()}/compare?path=${encodeURIComponent(item.path)}`);
    if (conflict.items[conflict.index] !== item) return;
    if (!res.ok) { status.textContent = await detail(res); return; }
    const data = await res.json();
    if (data.error) { status.textContent = data.error; return; }
    const max = Math.max(data.old.seconds, data.new.seconds);
    const waves = [];
    for (const [label, side, color] of [["기존", data.old, "#6c757d"], ["새", data.new, "#0d6efd"]]) {
        const canvas = el("canvas", "conflict-wave border-bottom");
        body.append(el("div", "small", `${label} ${side.seconds.toFixed(2)}초`), canvas);
        waves.push(() => drawWave(canvas, side.peaks, side.seconds, max, color));
    }
    waves.forEach((f) => f());
    const fmt = (v) => v == null ? "계산 불가(무음)" : v.toFixed(3);
    const sync = data.offset_ms ? `${Math.abs(data.offset_ms)}ms (새 파일이 ${data.offset_ms > 0 ? "빠름" : "늦음"})` : "0ms";
    status.textContent = `판정 상관계수 ${fmt(data.correlation)} (싱크 보정 없음, 같은 소재 기준 ${data.min_correlation} 이상, 길이 차이 2%·0.1초 이내)`
        + ` · 싱크 맞춘 상관계수 ${fmt(data.aligned_correlation)} · 싱크 오차 ${sync}`
        + (data.same ? " · 지금 기준으로는 같은 소재" : "");
}

// ---- 영상: 두 영상을 같이 재생하고 다른 구간을 프레임 단위 타임라인으로 ----

async function showVideos(item, left, right, body) {
    const status = el("p", "small text-muted", "영상을 바꾸고 프레임을 비교하는 중… (처음 한 번만, 길면 몇십 초)");
    body.append(status);
    const res = await fetch(`${conflictBase()}/compare?path=${encodeURIComponent(item.path)}`);
    if (conflict.items[conflict.index] !== item) return;
    if (!res.ok) { status.textContent = await detail(res); return; }
    const data = await res.json();
    if (data.error) { status.textContent = data.error; return; }
    const fps = data.fps;
    const videos = [conflictUrl(item, "old"), conflictUrl(item, "new")].map((url) => {
        const v = el("video", "conflict-media d-block w-100 bg-black");
        v.muted = true;
        v.preload = "auto";
        v.src = url;
        return v;
    });
    left.append(videos[0], el("div", "small text-muted", `${data.old_frames}프레임`));
    right.append(videos[1], el("div", "small text-muted", `${data.new_frames}프레임`));
    const total = data.diff.length;
    status.textContent = `${fps}fps로 맞춰 비교 · 다른 구간 ${data.segments.length}개 (프레임 평균 차이 ${(data.threshold * 100).toFixed(0)}% 넘음)`;

    const timeline = el("canvas", "conflict-timeline border");
    const controls = el("div", "d-flex flex-wrap align-items-center gap-2 my-2");
    const play = el("button", "btn btn-outline-secondary btn-sm", "▶ 재생");
    play.type = "button";
    const prev = el("button", "btn btn-outline-secondary btn-sm", "◀ 1프레임");
    prev.type = "button";
    const next = el("button", "btn btn-outline-secondary btn-sm", "1프레임 ▶");
    next.type = "button";
    const pos = el("span", "small font-monospace");
    controls.append(play, prev, next, pos);
    const segs = el("div", "d-flex flex-wrap gap-1");
    body.append(timeline, controls, segs);

    let frame = 0;
    const seek = (f) => {
        frame = Math.max(0, Math.min(f, total - 1));
        // 프레임 가운데로 옮겨야 앞 프레임이 보이지 않습니다.
        const t = (frame + 0.5) / fps;
        for (const v of videos) if (Math.abs(v.currentTime - t) > 0.5 / fps) v.currentTime = Math.min(t, v.duration || t);
        drawTimeline();
    };
    const drawTimeline = () => {
        const ratio = window.devicePixelRatio || 1;
        timeline.width = timeline.clientWidth * ratio;
        timeline.height = timeline.clientHeight * ratio;
        const ctx = timeline.getContext("2d");
        const w = timeline.width, h = timeline.height;
        ctx.clearRect(0, 0, w, h);
        const step = w / total;
        for (const [a, b] of data.segments) {
            ctx.fillStyle = "rgba(220, 53, 69, .25)";
            ctx.fillRect(a * step, 0, Math.max(1, (b - a + 1) * step), h);
        }
        ctx.fillStyle = "#6c757d";
        // 프레임이 픽셀보다 많으면 픽셀마다 가장 큰 차이를 그립니다.
        const cols = Math.min(total, Math.floor(w));
        for (let x = 0; x < cols; x++) {
            const s = Math.floor(x * total / cols), e = Math.max(s + 1, Math.floor((x + 1) * total / cols));
            let m = 0;
            for (let i = s; i < e; i++) m = Math.max(m, data.diff[i]);
            ctx.fillStyle = m > data.threshold ? "#dc3545" : "#6c757d";
            const bh = Math.max(1, Math.min(1, m) * h);
            ctx.fillRect(x * w / cols, h - bh, Math.max(1, w / cols), bh);
        }
        ctx.fillStyle = "#0d6efd";
        ctx.fillRect(frame * step, 0, Math.max(2, step), h);
        pos.textContent = `프레임 ${frame}/${total - 1} · ${(frame / fps).toFixed(2)}초 · 차이 ${(data.diff[frame] * 100).toFixed(1)}%`;
    };
    for (const [i, [a, b]] of data.segments.entries()) {
        if (i >= 50) { segs.append(el("span", "small text-muted", `외 ${data.segments.length - 50}개`)); break; }
        const btn = el("button", "btn btn-outline-danger btn-sm py-0", `${(a / fps).toFixed(2)}초 (${a}–${b})`);
        btn.type = "button";
        btn.addEventListener("click", () => seek(a));
        segs.append(btn);
    }
    timeline.addEventListener("click", (e) => {
        const rect = timeline.getBoundingClientRect();
        seek(Math.floor((e.clientX - rect.left) / rect.width * total));
    });
    const toggle = () => {
        if (videos[0].paused) {
            const t = videos[0].currentTime;
            videos[1].currentTime = t;
            for (const v of videos) v.play().catch(() => {});
            play.textContent = "❚❚ 멈춤";
        } else {
            for (const v of videos) v.pause();
            play.textContent = "▶ 재생";
            seek(Math.floor(videos[0].currentTime * fps));
        }
    };
    play.addEventListener("click", toggle);
    prev.addEventListener("click", () => { for (const v of videos) v.pause(); seek(frame - 1); });
    next.addEventListener("click", () => { for (const v of videos) v.pause(); seek(frame + 1); });
    // 재생 중에는 기존 영상을 기준으로 위치를 표시하고, 새 영상이 어긋나면 맞춥니다.
    videos[0].addEventListener("timeupdate", () => {
        if (videos[0].paused) return;
        frame = Math.min(total - 1, Math.floor(videos[0].currentTime * fps));
        if (Math.abs(videos[1].currentTime - videos[0].currentTime) > 0.1) videos[1].currentTime = videos[0].currentTime;
        drawTimeline();
    });
    videos[0].addEventListener("ended", () => { videos[1].pause(); play.textContent = "▶ 재생"; });
    conflictKeys = (e) => {
        if (e.key === "ArrowLeft") { prev.click(); return true; }
        if (e.key === "ArrowRight") { next.click(); return true; }
        if (e.key === " ") { toggle(); return true; }
        return false;
    };
    drawTimeline();
    if (data.segments.length) seek(data.segments[0][0]);
}

// ---- 그 밖의 파일: 줄 단위 diff ----

async function showText(item, body) {
    const status = el("p", "small text-muted", "비교하는 중…");
    body.append(status);
    const res = await fetch(`${conflictBase()}/compare?path=${encodeURIComponent(item.path)}`);
    if (conflict.items[conflict.index] !== item) return;
    if (!res.ok) { status.textContent = await detail(res); return; }
    const data = await res.json();
    if (data.binary) {
        status.textContent = `텍스트가 아니라 줄 단위로 비교하지 않습니다. 기존 ${size(data.old.size)} (sha256 ${data.old.sha256.slice(0, 12)}…) · 새 ${size(data.new.size)} (sha256 ${data.new.sha256.slice(0, 12)}…)`;
        return;
    }
    status.textContent = "빨강: 기존에만 있는 줄 · 초록: 새 파일에만 있는 줄";
    const pre = el("pre", "conflict-diff border rounded p-2");
    for (const line of data.diff.split("\n")) {
        const cls = line.startsWith("@@") ? "hunk" : line.startsWith("+") && !line.startsWith("+++") ? "add"
            : line.startsWith("-") && !line.startsWith("---") ? "del" : "";
        pre.append(el("div", cls, line || " "));
    }
    body.append(pre);
}

// ---- 단축키·적용 ----

document.addEventListener("keydown", (e) => {
    if (!conflict || !conflictModalEl.classList.contains("show")) return;
    if (e.target.closest("input, textarea, select") || e.ctrlKey || e.metaKey || e.altKey) return;
    const item = conflict.items[conflict.index];
    if (conflictKeys && conflictKeys(e)) { e.preventDefault(); return; }
    if (e.key === "1" && item) choose(item, "old").then((ok) => ok && nextUnchosen());
    else if (e.key === "2" && item) choose(item, "new").then((ok) => ok && nextUnchosen());
    else if (e.key === "j") showConflict(conflict.index + 1);
    else if (e.key === "k") showConflict(conflict.index - 1);
    else return;
    e.preventDefault();
});

// 고른 뒤 다음 고르지 않은 파일로 넘어갑니다.
function nextUnchosen() {
    const n = conflict.items.length;
    for (let k = 1; k <= n; k++) {
        const i = (conflict.index + k) % n;
        if (!conflict.items[i].choice) { showConflict(i); return; }
    }
}

conflictAllOld.addEventListener("click", async () => {
    const rest = conflict.items.filter((i) => !i.choice);
    if (!rest.length) return;
    const res = await fetch(conflictBase(), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ choices: Object.fromEntries(rest.map((i) => [i.path, "old"])) }),
    });
    if (!res.ok) { alert(await detail(res)); return; }
    for (const i of rest) i.choice = "old";
    renderConflictList();
    renderChoice(conflict.items[conflict.index]);
});

conflictApply.addEventListener("click", async () => {
    const changes = conflict.items.filter((i) => i.choice === "new").length;
    if (!confirm(`이 곡만 다시 넣습니다. 새 파일로 바꿈 ${changes}개, 기존 유지 ${conflict.items.length - changes}개.`)) return;
    conflictApply.disabled = true;
    try {
        await postJson(`${conflictBase()}/apply?slow=${document.getElementById("slow-commit").checked}`);
    } catch (err) {
        alert(err.message);
        conflictApply.disabled = false;
        return;
    }
    conflict = null;  // 묶음을 다시 읽는 일은 아래에서 합니다(창이 닫힐 때 또 읽지 않게).
    conflictModal.hide();
    refreshJobs();
    await loadStage(stagePage);
    loadStages();
});

conflictModalEl.addEventListener("hidden.bs.modal", () => {
    for (const m of conflictView.querySelectorAll("video, audio")) m.pause();
    conflictView.replaceChildren();
    conflictKeys = null;
    const changed = conflict;
    conflict = null;
    // 고른 수가 곡 행 버튼에 보이도록 다시 읽습니다.
    if (changed && currentStage?.id === changed.stageId) loadStage(stagePage);
});
