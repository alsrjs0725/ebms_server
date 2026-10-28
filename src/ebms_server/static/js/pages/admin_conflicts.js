// 곡 임포트 검토의 병합 충돌 해결 창. admin_songs.js 다음에 읽어 그 전역(currentStage, loadStage, detail, size 등)을 씁니다.
// 충돌 파일마다 기존 곡의 파일과 새 파일을 나란히 비교하고 old(기존 유지)·new(새 파일로 바꿈)를 고른 뒤 그 곡만 다시 넣습니다.

const conflictModalEl = document.getElementById("conflict-modal");
const conflictModal = new bootstrap.Modal(conflictModalEl);
const conflictList = document.getElementById("conflict-list");
const conflictView = document.getElementById("conflict-view");
const conflictCount = document.getElementById("conflict-count");
const conflictApply = document.getElementById("conflict-apply");
const conflictAllOld = document.getElementById("conflict-all-old");
const conflictAi = document.getElementById("conflict-ai");
const conflictAiSummary = document.getElementById("conflict-ai-summary");
const KIND = { sound: "키음", image: "이미지", video: "영상", chart: "차트", other: "파일" };

let conflict = null;  // {stageId, unit, items, songs, index}
let conflictKeys = null;  // 지금 보는 파일의 단축키 처리(영상 프레임 이동 등)

// 곡 행에 붙일 "충돌 해결" 버튼. 고를 충돌 파일이 없으면 null.
function conflictButton(stage, unit) {
    const paths = unit.result?.conflicts || [];
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
    conflictAiSummary.classList.add("d-none");
    conflictModal.show();
    const res = await fetch(conflictBase());
    if (!res.ok) { conflictList.textContent = await detail(res); return; }
    const data = await res.json();
    conflict.items = data.items;
    conflict.songs = data.songs;
    renderAiSummary(data.ai);
    const first = conflict.items.findIndex((i) => !i.choice);
    renderConflictList();
    showConflict(first < 0 ? 0 : first);
}

function choiceText(choice) {
    return choice === "old" ? "기존 유지" : choice === "new" ? "새 파일" : "고르지 않음";
}

// MCP(AI)가 남긴 곡 전체 판단. 추천은 저장만 돼 있고, 고르는 것은 관리자가 합니다.
function renderAiSummary(ai) {
    conflictAiSummary.replaceChildren();
    conflictAiSummary.classList.toggle("d-none", !ai);
    if (!ai) return;
    conflictAiSummary.append(el("strong", "", `AI 판단${ai.model ? ` (${ai.model})` : ""}: `), ai.summary || "");
    if (ai.split_recommended) conflictAiSummary.append(el("div", "fw-semibold", "다른 곡이 섞여 있어 새 곡으로 나누는 것을 권합니다."));
    if (ai.issues?.length) {
        const ul = el("ul", "mb-0");
        ul.append(...ai.issues.map((t) => el("li", "", t)));
        conflictAiSummary.append(ul);
    }
}

function aiText(ai) {
    return ai.choice === "unsure" ? "모르겠음" : choiceText(ai.choice);
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
        const badges = el("span", "d-flex gap-1 flex-shrink-0");
        if (item.ai) {
            const ai = el("span", `badge ${item.ai.choice === "unsure" ? "text-bg-warning" : "text-bg-info"}`, `AI ${aiText(item.ai)}`);
            ai.title = item.ai.reason || "";
            badges.append(ai);
        }
        badges.append(badge);
        a.append(name, badges);
        a.addEventListener("click", () => showConflict(i));
        return a;
    }));
    const done = conflict.items.filter((i) => i.choice).length;
    conflictCount.textContent = `고름 ${done}/${conflict.items.length}`;
    conflictApply.disabled = !conflict.items.length || done < conflict.items.length;
    conflictAi.classList.toggle("d-none", !aiPending().length);
}

// 고르지 않은 파일 중 AI가 old·new로 추천한 것(unsure는 사람이 고릅니다).
function aiPending() {
    return conflict.items.filter((i) => !i.choice && (i.ai?.choice === "old" || i.ai?.choice === "new"));
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
    stopConflictPlayer();
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
    if (item.ai) {
        const confidence = item.ai.confidence == null ? "" : ` ${Math.round(item.ai.confidence * 100)}%`;
        const line = el("div", "small w-100 text-body-secondary");
        line.append(el("span", `badge me-1 ${item.ai.choice === "unsure" ? "text-bg-warning" : "text-bg-info"}`, `AI ${aiText(item.ai)}${confidence}`),
            item.ai.reason || "");
        head.append(line);
    }

    const cols = el("div", "row g-2");
    const left = el("div", "col-6");
    const right = el("div", "col-6");
    left.append(sideHeader("기존", item.old_path || "(없음)", item.old_size, conflictUrl(item, "old", "&raw=true")));
    right.append(sideHeader("새", item.path, item.new_size, conflictUrl(item, "new", "&raw=true")));
    cols.append(left, right);
    const body = el("div", "mt-2");
    conflictView.replaceChildren(head, cols, body);
    if (item.kind === "chart") {
        body.append(el("p", "small text-muted",
            "기존 유지: 새 차트는 등록하지 않습니다. 새 파일로 바꿈: 곡 파일의 차트를 새 것으로 바꾸고 새 차트를 등록합니다(기존 차트 등록은 그대로)."));
    }
    renderChoice(item);

    const oldKind = item.old_kind || item.kind;
    if (item.kind === "image" && oldKind === "image") showImages(item, left, right, body);
    else if (item.kind === "sound" && oldKind === "sound") showSounds(item, left, right, body);
    else if (item.kind === "video" && oldKind === "video") showVideos(item, left, right, body);
    else if (item.kind === oldKind && (item.kind === "chart" || item.kind === "other")) showText(item, body);
    else {
        // 같은 BGA 자리에 이미지와 영상: 각자 보여 주기만 합니다.
        left.append(mediaElement(oldKind, conflictUrl(item, "old")));
        right.append(mediaElement(item.kind, conflictUrl(item, "new")));
        body.append(el("p", "small text-muted", "이미지와 영상이라 차이를 계산하지 않습니다."));
    }
    if (item.kind === oldKind && (item.kind === "sound" || item.kind === "chart")) chartListen(item);
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

// ---- 채보로 들어보기: 바탕 음원(충돌 없는 소리) 위에 충돌 키음을 차트 시각에 맞춰 겹쳐 재생 ----
// 키음 충돌은 같은 위치에서 기존(A)·새(B) 키음을, 차트 충돌은 기존(A)·새(B) 차트를 바꿔 듣습니다.
// 다른 충돌 키음은 고른 쪽(고르지 않았으면 기존)으로 울립니다. 키음은 재생 위치 앞 LISTEN_AHEAD초 안에 울릴 것만 받아 둡니다.

const LISTEN_AHEAD = 10;
const LISTEN_KEEP = 30;  // 재생 위치에서 이만큼 먼 키음은 메모리에서 뺍니다.
const LISTEN_SCHEDULE = 0.3;  // 이만큼 앞서 재생을 예약합니다(초).
const LISTEN_LOOP = 2;  // 눈금을 누르면 그 앞뒤 이만큼(초)을 반복합니다.

let conflictPlayer = null;

function stopConflictPlayer() {
    if (conflictPlayer) conflictPlayer.close();
    conflictPlayer = null;
}

function clock(t) {
    const m = Math.floor(t / 60);
    return `${m}:${(t - m * 60).toFixed(1).padStart(4, "0")}`;
}

function chartListen(item) {
    const box = el("div", "border rounded p-2 mt-3");
    const head = el("div", "d-flex flex-wrap align-items-center gap-2");
    head.append(el("strong", "", "채보로 들어보기"));
    const start = el("button", "btn btn-sm btn-outline-primary", "채보 음원 만들기");
    start.type = "button";
    head.append(start);
    const status = el("div", "small text-muted mt-1",
        item.kind === "chart" ? "기존 차트(A)와 새 차트(B)를 같은 위치에서 바꿔 들어 봅니다."
            : "이 키음을 쓰는 차트를 재생하며 기존 키음(A)과 새 키음(B)을 같은 위치에서 바꿔 들어 봅니다.");
    box.append(head, status);
    conflictView.append(box);
    start.addEventListener("click", () => { start.remove(); setupListen(item, box, head, status); });
}

async function waitRender(item, chart, status) {
    const q = `song=${item.song_id}&side=${chart.side}&chart=${encodeURIComponent(chart.path)}`;
    for (;;) {
        const res = await fetch(`${conflictBase()}/render?${q}`);
        if (!res.ok) throw new Error(await detail(res));
        const data = await res.json();
        if (data.status === "error") throw new Error(data.error);
        if (data.status === "ready") return { data, bedUrl: `${conflictBase()}/render/bed?${q}` };
        status.textContent = `채보 음원 만드는 중… 키음 ${data.done}/${data.total || "?"}`;
        await new Promise((r) => setTimeout(r, 1000));
    }
}

async function setupListen(item, box, head, status) {
    status.textContent = "차트를 찾는 중…";
    const res = await fetch(`${conflictBase()}/charts?path=${encodeURIComponent(item.path)}`);
    if (!res.ok) { status.textContent = await detail(res); return; }
    const charts = (await res.json()).items;
    if (!charts.length) { status.textContent = "이 키음을 쓰는 차트가 없습니다."; return; }
    if (!conflict || conflict.items[conflict.index] !== item) return;
    let ctx;
    try {
        ctx = new AudioContext({ sampleRate: 44100 });
    } catch {
        ctx = new AudioContext();
    }
    stopConflictPlayer();
    const player = makePlayer(item, ctx, box, status);
    conflictPlayer = player;
    if (item.kind === "chart") {
        // A·B는 차트의 기존·새 쪽입니다.
        const pick = (side) => charts.find((c) => c.side === side);
        if (!pick("old") || !pick("new")) { status.textContent = "기존·새 차트를 읽지 못했습니다."; return; }
        await player.load({ old: pick("old"), new: pick("new") });
        return;
    }
    // 키음 충돌: 차트를 고르고, A·B는 그 차트 안의 이 키음입니다. 기본은 이 키음을 가장 많이 쓰는 새 차트.
    const select = el("select", "form-select form-select-sm w-auto");
    select.setAttribute("aria-label", "들어볼 차트");
    charts.forEach((c, i) => {
        const o = el("option", "", `${c.side === "old" ? "기존" : "새"} ${c.path}${c.title ? ` · ${c.title}` : ""} · ${c.uses}번`);
        o.value = i;
        select.append(o);
    });
    const best = charts.reduce((b, c, i) => (c.side === "new" && (b < 0 || c.uses > charts[b].uses) ? i : b), -1);
    select.value = best < 0 ? 0 : best;
    head.append(select);
    select.addEventListener("change", () => player.load({ both: charts[select.value] }));
    await player.load({ both: charts[select.value] });
}

function makePlayer(item, ctx, box, status) {
    const chartMode = item.kind === "chart";
    const master = ctx.createGain();
    master.connect(ctx.destination);
    const bedGain = ctx.createGain();
    const overlayGain = ctx.createGain();  // 바탕과 같은 gain
    const keyGain = ctx.createGain();  // 지금 보는 키음
    bedGain.connect(master);
    overlayGain.connect(master);
    keyGain.connect(overlayGain);

    const renders = {};  // {old, new} 또는 {both}: {data, bed}
    const buffers = new Map();  // "키|old" → AudioBuffer | Promise | null(못 읽음)
    const sources = new Set();
    let ab = "new";
    let current = null;
    let playing = false, pos = 0, t0ctx = 0, t0pos = 0, next = 0, timer = null, loop = null, closed = false, failed = 0;

    // ---- 화면 ----
    const controls = el("div", "d-flex flex-wrap align-items-center gap-2 mt-2");
    const playBtn = el("button", "btn btn-sm btn-primary", "재생");
    playBtn.type = "button";
    const time = el("span", "small font-monospace", "");
    const abGroup = el("div", "btn-group btn-group-sm");
    abGroup.setAttribute("role", "group");
    const abBtns = {};
    for (const [value, text] of [["old", chartMode ? "A 기존 차트" : "A 기존 키음"], ["new", chartMode ? "B 새 차트" : "B 새 키음"]]) {
        const b = el("button", "btn btn-outline-secondary", text);
        b.type = "button";
        b.addEventListener("click", () => setAb(value));
        abBtns[value] = b;
        abGroup.append(b);
    }
    const pickBtn = el("button", "btn btn-sm btn-outline-primary", "지금 듣는 쪽으로 고르기");
    pickBtn.type = "button";
    pickBtn.addEventListener("click", () => choose(item, ab));
    const loopBtn = el("button", "btn btn-sm btn-outline-warning d-none", "반복 끄기");
    loopBtn.type = "button";
    loopBtn.addEventListener("click", () => { loop = null; loopBtn.classList.add("d-none"); draw(); });
    controls.append(playBtn, time, abGroup, pickBtn, loopBtn);
    const mixRow = el("div", "d-flex flex-wrap align-items-center gap-3 mt-2 small");
    const slider = (label, value, max, gain) => {
        const wrap = el("label", "d-flex align-items-center gap-1", label);
        const r = el("input", "form-range");
        r.type = "range"; r.min = 0; r.max = max; r.value = value; r.style.width = "8rem";
        r.addEventListener("input", () => { gain.gain.value = r.value / 100; });
        wrap.append(r);
        return wrap;
    };
    mixRow.append(slider("바탕 음량", 100, 100, bedGain));
    if (!chartMode) mixRow.append(slider("이 키음 음량", 100, 300, keyGain));
    const canvas = el("canvas", "w-100 border rounded mt-2 conflict-timeline");
    canvas.style.height = "48px";
    canvas.style.cursor = "pointer";
    const help = el("div", "small text-muted mt-1",
        (chartMode ? "" : "빨간 눈금: 이 키음이 울리는 곳(누르면 앞뒤 2초 반복) · ") + "Space 재생/멈춤 · A/B 바꾸기 · ←/→ 이전/다음 눈금");
    box.append(controls, mixRow, canvas, help);

    const otherChoice = (key) => conflict.items.find((i) => i.path === key)?.choice || "old";
    const variant = (key) => (!chartMode && key === item.path ? ab : otherChoice(key));
    const now = () => (playing ? t0pos + (ctx.currentTime - t0ctx) : pos);
    const events = () => current?.data.events || [];
    const markers = () => (chartMode ? [] : events().filter((e) => e.key === item.path).map((e) => e.t));

    function renderAb() {
        for (const [v, b] of Object.entries(abBtns)) b.classList.toggle("active", v === ab);
    }

    function draw() {
        const ratio = window.devicePixelRatio || 1;
        canvas.width = canvas.clientWidth * ratio;
        canvas.height = canvas.clientHeight * ratio;
        const g = canvas.getContext("2d");
        g.clearRect(0, 0, canvas.width, canvas.height);
        if (!current) return;
        const total = current.data.seconds || 1;
        const x = (t) => (t / total) * canvas.width;
        if (loop) {
            g.fillStyle = "rgba(255,193,7,0.3)";
            g.fillRect(x(loop[0]), 0, Math.max(2, x(loop[1]) - x(loop[0])), canvas.height);
        }
        g.fillStyle = "#dc3545";
        for (const t of markers()) g.fillRect(x(t), canvas.height * 0.2, Math.max(1, ratio), canvas.height * 0.6);
        g.fillStyle = "#0d6efd";
        g.fillRect(x(now()), 0, 2 * ratio, canvas.height);
        time.textContent = `${clock(now())} / ${clock(total)}${failed ? ` · 못 읽은 키음 ${failed}` : ""}`;
    }

    function stopSources() {
        for (const s of sources) { try { s.stop(); } catch { /* 이미 끝남 */ } }
        sources.clear();
    }

    function start(buffer, when, offset, duration, dest) {
        if (offset >= buffer.duration) return;
        const s = ctx.createBufferSource();
        s.buffer = buffer;
        s.connect(dest);
        s.onended = () => sources.delete(s);
        sources.add(s);
        if (duration === undefined) s.start(when, offset);
        else if (duration > 0) s.start(when, offset, duration);
    }

    // 재생 위치 p 앞 LISTEN_AHEAD초 안에 울릴 키음을 받고, 먼 키음은 뺍니다.
    function ensure(p) {
        const want = new Set();
        for (const e of events()) {
            if (e.t < p - 1) continue;
            if (e.t > p + LISTEN_AHEAD) break;
            if (!chartMode && e.key === item.path) { want.add(`${e.key}|old`); want.add(`${e.key}|new`); }
            else want.add(`${e.key}|${variant(e.key)}`);
        }
        const keep = new Set(want);
        for (const e of events()) {
            if (e.t > p - 5 && e.t < p + LISTEN_KEEP) keep.add(`${e.key}|${variant(e.key)}`);
        }
        for (const id of buffers.keys()) {
            if (!keep.has(id) && !id.startsWith(`${item.path}|`)) buffers.delete(id);
        }
        const pending = [];
        for (const id of want) {
            if (buffers.has(id)) { const b = buffers.get(id); if (b instanceof Promise) pending.push(b); continue; }
            const [key, side] = [id.slice(0, id.lastIndexOf("|")), id.slice(id.lastIndexOf("|") + 1)];
            const promise = fetch(`${conflictBase()}/file?path=${encodeURIComponent(key)}&side=${side}&raw=true`)
                .then((r) => (r.ok ? r.arrayBuffer() : Promise.reject(new Error(r.statusText))))
                .then((data) => ctx.decodeAudioData(data))
                .then((buf) => { if (buffers.get(id) === promise) buffers.set(id, buf); return buf; })
                .catch(() => { if (buffers.get(id) === promise) { buffers.set(id, null); failed++; } return null; });
            buffers.set(id, promise);
            pending.push(promise);
        }
        return pending;
    }

    function bufferFor(e) {
        const b = buffers.get(`${e.key}|${variant(e.key)}`);
        return b instanceof AudioBuffer ? b : null;
    }

    function dest(e) {
        if (!chartMode && e.key === item.path) return keyGain;
        return overlayGain;
    }

    function playFrom(from) {
        stopSources();
        if (!current) return;
        from = Math.max(0, Math.min(from, current.data.seconds));
        t0ctx = ctx.currentTime + 0.05;
        t0pos = from;
        playing = true;
        playBtn.textContent = "멈춤";
        start(current.bed, t0ctx, from, undefined, bedGain);
        const list = events();
        // from 앞에서 울리기 시작해 아직 울리는 소리도 이어서 냅니다.
        next = list.findIndex((e) => e.t >= from);
        if (next < 0) next = list.length;
        for (let i = next - 1; i >= 0 && list[i].t > from - 30; i--) {
            const e = list[i], b = bufferFor(e);
            if (!b || (e.e != null && e.e <= from)) continue;
            start(b, t0ctx, e.o + (from - e.t), e.e != null ? e.e - from : undefined, dest(e));
        }
        if (!timer) timer = setInterval(tick, 50);
    }

    // to로 옮깁니다. play면(기본은 재생 중이었으면) 이어서 재생합니다.
    async function seek(to, play = playing) {
        pause();
        pos = Math.max(0, Math.min(to, current ? current.data.seconds : 0));
        draw();
        if (!play) { ensure(pos); return; }
        // 옮긴 자리의 키음을 잠깐 기다렸다 재생합니다(최대 3초).
        await Promise.race([Promise.all(ensure(pos)), new Promise((r) => setTimeout(r, 3000))]);
        if (!closed) playFrom(pos);
    }

    function pause() {
        if (playing) pos = now();
        playing = false;
        stopSources();
        playBtn.textContent = "재생";
    }

    function tick() {
        if (!current) return;
        const p = now();
        ensure(p);
        if (playing && loop && p >= loop[1]) { playFrom(loop[0]); return; }
        if (playing && p >= current.data.seconds) { pause(); pos = 0; }
        if (playing) {
            const list = events();
            const gain = current.data.gain;
            overlayGain.gain.value = gain;
            while (next < list.length && list[next].t < p + LISTEN_SCHEDULE) {
                const e = list[next++];
                const b = bufferFor(e);
                if (!b) continue;
                const when = t0ctx + (e.t - t0pos);
                const late = Math.max(0, ctx.currentTime - when);
                start(b, when + late, e.o + late, e.e != null ? e.e - e.t - late : undefined, dest(e));
            }
        }
        draw();
    }

    function setAb(value) {
        if (value === ab) return;
        ab = value;
        renderAb();
        const p = now();
        if (chartMode) current = renders[ab];
        if (!current) return;
        overlayGain.gain.value = current.data.gain;
        if (playing) playFrom(Math.min(p, current.data.seconds));
        else { pos = Math.min(p, current.data.seconds); draw(); }
    }

    function marker(dir) {
        const p = now(), list = markers();
        const t = dir > 0 ? list.find((x) => x > p + 0.05) : [...list].reverse().find((x) => x < p - 0.3);
        if (t === undefined) return;
        loop = [Math.max(0, t - LISTEN_LOOP), t + LISTEN_LOOP];
        loopBtn.classList.remove("d-none");
        seek(loop[0], true);
    }

    playBtn.addEventListener("click", () => (playing ? pause() : seek(pos, true)));
    canvas.addEventListener("click", (ev) => {
        if (!current) return;
        const rect = canvas.getBoundingClientRect();
        const t = ((ev.clientX - rect.left) / rect.width) * current.data.seconds;
        const near = markers().find((m) => Math.abs(((m - t) / current.data.seconds) * rect.width) <= 4);
        if (near !== undefined) {
            loop = [Math.max(0, near - LISTEN_LOOP), near + LISTEN_LOOP];
            loopBtn.classList.remove("d-none");
            seek(loop[0], true);
        } else {
            seek(t);
        }
    });
    conflictKeys = (e) => {
        if (!current) return false;
        if (e.key === " ") { if (playing) pause(); else seek(pos, true); return true; }
        if (e.key === "a" || e.key === "A") { setAb("old"); return true; }
        if (e.key === "b" || e.key === "B") { setAb("new"); return true; }
        if (!chartMode && e.key === "ArrowLeft") { marker(-1); return true; }
        if (!chartMode && e.key === "ArrowRight") { marker(1); return true; }
        return false;
    };
    renderAb();

    return {
        // charts: {old, new}(차트 충돌) 또는 {both}(키음 충돌: 차트 하나).
        async load(charts) {
            pause();
            current = null;
            loop = null;
            loopBtn.classList.add("d-none");
            for (const k of Object.keys(renders)) delete renders[k];
            try {
                for (const [k, chart] of Object.entries(charts)) {
                    const { data, bedUrl } = await waitRender(item, chart, status);
                    if (closed) return;
                    status.textContent = "바탕 음원을 받는 중…";
                    const r = await fetch(bedUrl);
                    if (!r.ok) throw new Error(await detail(r));
                    const bed = await ctx.decodeAudioData(await r.arrayBuffer());
                    if (closed) return;
                    renders[k] = { data, bed };
                }
            } catch (err) {
                status.textContent = err.message;
                return;
            }
            current = chartMode ? renders[ab] : renders.both;
            overlayGain.gain.value = current.data.gain;
            const list = markers();
            status.textContent = chartMode ? "A/B로 기존·새 차트를 바꿔 들어 보세요."
                : `이 키음이 ${list.length}번 울립니다. 눈금을 누르면 그 앞뒤를 반복합니다.`;
            pos = list.length ? Math.max(0, list[0] - LISTEN_LOOP) : 0;
            ensure(pos);
            draw();
        },
        close() {
            closed = true;
            pause();
            clearInterval(timer);
            buffers.clear();
            conflictKeys = null;
            ctx.close();
        },
    };
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

conflictAi.addEventListener("click", async () => {
    const rest = aiPending();
    if (!rest.length) return;
    const unsure = conflict.items.filter((i) => !i.choice && !rest.includes(i)).length;
    if (!confirm(`AI 추천대로 ${rest.length}개를 고릅니다.${unsure ? ` 추천이 없거나 모르겠다고 한 ${unsure}개는 직접 고르세요.` : ""}`)) return;
    const res = await fetch(conflictBase(), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ choices: Object.fromEntries(rest.map((i) => [i.path, i.ai.choice])) }),
    });
    if (!res.ok) { alert(await detail(res)); return; }
    for (const i of rest) i.choice = i.ai.choice;
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
    stopConflictPlayer();
    for (const m of conflictView.querySelectorAll("video, audio")) m.pause();
    conflictView.replaceChildren();
    conflictKeys = null;
    const changed = conflict;
    conflict = null;
    // 고른 수가 곡 행 버튼에 보이도록 다시 읽습니다.
    if (changed && currentStage?.id === changed.stageId) loadStage(stagePage);
});
