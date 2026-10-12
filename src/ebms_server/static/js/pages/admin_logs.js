const form = document.getElementById("filter");
const rows = document.getElementById("rows");
const more = document.getElementById("more");
const info = document.getElementById("info");
const BADGE = { DEBUG: "text-bg-light", INFO: "text-bg-secondary", WARNING: "text-bg-warning", ERROR: "text-bg-danger", CRITICAL: "text-bg-dark" };

// 주소의 ?level=&logger=&q= 로 필터를 채워 링크로 공유할 수 있게 합니다.
const params = new URLSearchParams(location.search);
for (const name of ["level", "logger", "q"]) {
    const el = form.elements[name];
    const value = params.get(name);
    if (!value) continue;
    if (el.tagName === "SELECT" && ![...el.options].some((o) => o.value === value)) el.add(new Option(value, value));
    el.value = value;
}

function filters() {
    const p = new URLSearchParams();
    for (const name of ["level", "logger", "q"]) {
        const value = form.elements[name].value.trim();
        if (value) p.set(name, value);
    }
    return p;
}

function row(entry) {
    const tr = document.createElement("tr");
    const time = document.createElement("td");
    time.className = "text-nowrap";
    time.textContent = entry.time;
    const level = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "badge " + (BADGE[entry.level] || "text-bg-secondary");
    badge.textContent = entry.level;
    level.append(badge);
    const logger = document.createElement("td");
    logger.className = "text-break";
    logger.textContent = entry.logger;
    const msg = document.createElement("td");
    msg.className = "text-break";
    msg.style.whiteSpace = "pre-wrap";
    msg.textContent = entry.message;
    if (entry.detail) {
        const d = document.createElement("details");
        const s = document.createElement("summary");
        s.textContent = "자세히";
        const pre = document.createElement("pre");
        pre.className = "small mb-0";
        pre.textContent = entry.detail;
        d.append(s, pre);
        msg.append(d);
    }
    tr.append(time, level, logger, msg);
    return tr;
}

let cursor = null;
let seq = 0;
async function load(append) {
    const mine = ++seq;
    const p = filters();
    history.replaceState(null, "", p.toString() ? `?${p}` : location.pathname);
    if (append && cursor) p.set("cursor", cursor);
    more.disabled = true;
    let data;
    try {
        const res = await fetch(`/api/admin/logs?${p}`);
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
        data = await res.json();
    } catch (err) {
        if (mine === seq) info.textContent = `불러오지 못했습니다: ${err.message}`;
        return;
    } finally {
        more.disabled = false;
    }
    if (mine !== seq) return;  // 더 최근 요청이 있으면 버립니다.
    if (!append) rows.replaceChildren();
    rows.append(...data.entries.map(row));
    cursor = data.next;
    more.classList.toggle("d-none", !cursor);
    const count = rows.children.length;
    if (!count && !cursor) info.textContent = "조건에 맞는 로그가 없습니다.";
    else if (cursor && data.scanned_to) info.textContent = `${count}개 · ${data.scanned_to}까지 확인했습니다. 더 오래된 로그는 더 보기를 누르세요.`;
    else info.textContent = cursor ? `${count}개 표시 중` : `${count}개 (끝까지 확인)`;
}

let typing = null;
form.addEventListener("submit", (e) => { e.preventDefault(); load(false); });
form.addEventListener("change", () => load(false));
form.elements.q.addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(() => load(false), 300); });
document.getElementById("refresh").addEventListener("click", () => load(false));
more.addEventListener("click", () => load(true));
load(false);
