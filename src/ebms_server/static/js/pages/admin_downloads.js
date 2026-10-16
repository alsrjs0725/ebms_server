const form = document.getElementById("filter");
const rows = document.getElementById("rows");
const info = document.getElementById("info");
const pager = document.getElementById("pager");
const FIELDS = ["user", "song", "result"];
// 결과 표시 이름은 결과 선택지에서 가져옵니다.
const LABELS = Object.fromEntries([...form.elements.result.options].filter((o) => o.value).map((o) => [o.value, o.text]));
const OK = new Set(["charged", "free"]);

// 주소의 ?user=&song=&result= 로 필터를 채워 링크로 공유할 수 있게 합니다(관리자 홈의 사용자별 링크).
const params = new URLSearchParams(location.search);
for (const name of FIELDS) {
    const value = params.get(name);
    if (value) form.elements[name].value = value;
}

function filters() {
    const p = new URLSearchParams();
    for (const name of FIELDS) {
        const value = form.elements[name].value.trim();
        if (value) p.set(name, value);
    }
    return p;
}

function cell(...children) {
    const td = document.createElement("td");
    td.className = "text-break";
    td.append(...children);
    return td;
}

function small(text) {
    const el = document.createElement("small");
    el.className = "text-muted d-block";
    el.textContent = text;
    return el;
}

function row(item) {
    const tr = document.createElement("tr");
    const time = cell(new Date(item.at * 1000).toLocaleString());
    time.className = "text-nowrap";
    const user = document.createElement("a");
    user.href = `?user=${encodeURIComponent(item.user_id)}`;
    user.textContent = item.display_name || "(이름 없음)";
    const song = document.createElement("a");
    song.href = `?song=${item.song_id}`;
    song.textContent = `#${item.song_id}`;
    const songCell = cell(song, " ", item.folder || "");
    if (item.song_deleted) songCell.append(" ", Object.assign(document.createElement("span"), { className: "badge text-bg-secondary", textContent: "사용 중지" }));
    const badge = document.createElement("span");
    badge.className = "badge " + (OK.has(item.result) ? "text-bg-success" : "text-bg-danger");
    badge.textContent = LABELS[item.result] || item.result;
    const result = cell(badge);
    if (item.resumed) result.append(small("이어받기"));
    tr.append(time, cell(user, small(item.user_id)), songCell, result);
    return tr;
}

function renderPager(data) {
    pager.replaceChildren();
    if (data.pages <= 1) return;
    const ul = document.createElement("ul");
    ul.className = "pagination pagination-sm";
    const item = (label, page, { active = false, disabled = false } = {}) => {
        const li = document.createElement("li");
        li.className = "page-item" + (active ? " active" : "") + (disabled ? " disabled" : "");
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "page-link";
        btn.textContent = label;
        if (!active && !disabled) btn.addEventListener("click", () => load(page));
        li.append(btn);
        ul.append(li);
    };
    const { page, pages } = data;
    item("‹", page - 1, { disabled: page === 1 });
    const from = Math.max(1, Math.min(page - 2, pages - 4));
    for (let p = from; p <= Math.min(pages, from + 4); p++) item(String(p), p, { active: p === page });
    item("›", page + 1, { disabled: page === pages });
    pager.append(ul);
}

let seq = 0;
async function load(page = 1) {
    const mine = ++seq;
    const p = filters();
    history.replaceState(null, "", p.toString() ? `?${p}` : location.pathname);
    p.set("page", page);
    let data;
    try {
        const res = await fetch(`/api/admin/downloads?${p}`);
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
        data = await res.json();
    } catch (err) {
        if (mine === seq) info.textContent = `불러오지 못했습니다: ${err.message}`;
        return;
    }
    if (mine !== seq) return;  // 더 최근 요청이 있으면 버립니다.
    rows.replaceChildren(...data.items.map(row));
    renderPager(data);
    info.textContent = data.total ? `${data.page} / ${data.pages}쪽 · 모두 ${data.total}개` : "조건에 맞는 기록이 없습니다.";
}

let typing = null;
form.addEventListener("submit", (e) => { e.preventDefault(); load(); });
form.addEventListener("change", () => load());
for (const name of ["user", "song"]) {
    form.elements[name].addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(() => load(), 300); });
}
document.getElementById("refresh").addEventListener("click", () => load());
// 링크(?user=, ?song=)는 같은 페이지에서 필터만 바꿉니다.
rows.addEventListener("click", (e) => {
    const a = e.target.closest("a");
    if (!a) return;
    e.preventDefault();
    for (const [name, value] of new URLSearchParams(a.search)) form.elements[name].value = value;
    load();
});
load();
