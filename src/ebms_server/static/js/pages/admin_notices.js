function pad(n) { return String(n).padStart(2, "0"); }

// unix 초 <-> datetime-local 값(브라우저 시간대)
function toLocal(unix) {
    const d = new Date(unix * 1000);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

document.querySelectorAll("input[data-unix]").forEach((input) => {
    if (input.dataset.unix) input.value = toLocal(Number(input.dataset.unix));
});
document.querySelectorAll("[data-time]").forEach((el) => {
    el.textContent = new Date(Number(el.dataset.time) * 1000).toLocaleString();
});

function values(form) {
    const unix = (name) => {
        const v = form.querySelector(`[name=${name}]`).value;
        return v ? Math.floor(new Date(v).getTime() / 1000) : null;
    };
    return {
        title: form.querySelector("[name=title]").value,
        body: form.querySelector("[name=body]").value,
        level: form.querySelector("[name=level]").value,
        published: form.querySelector("[name=published]").checked,
        starts_at: unix("starts_at"),
        ends_at: unix("ends_at"),
    };
}

async function send(method, url, body) {
    const res = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("\n") : data.detail;
        alert(detail || `실패했습니다 (${res.status})`);
        return false;
    }
    return true;
}

document.getElementById("create").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (await send("POST", "/api/admin/notices", values(e.target))) location.reload();
});

document.querySelectorAll("form.notice").forEach((form) => {
    const url = `/api/admin/notices/${form.dataset.id}`;
    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        if (await send("PUT", url, values(form))) location.reload();
    });
    form.querySelector("[data-delete]").addEventListener("click", async () => {
        if (!confirm("이 공지를 삭제할까요?")) return;
        if (await send("DELETE", url)) location.reload();
    });
});
