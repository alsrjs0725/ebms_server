document.querySelectorAll("[data-time]").forEach((el) => {
    el.textContent = new Date(Number(el.dataset.time) * 1000).toLocaleString();
});

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

async function busy(button, action) {
    button.disabled = true;
    try {
        if (await action()) location.reload();
    } finally {
        button.disabled = false;
    }
}

document.getElementById("create").addEventListener("submit", (e) => {
    e.preventDefault();
    const button = e.submitter;
    busy(button, () => send("POST", "/api/admin/tables", { url: e.target.querySelector("[name=url]").value }));
});

document.querySelectorAll("tr[data-id]").forEach((row) => {
    const url = `/api/admin/tables/${row.dataset.id}`;
    const refresh = row.querySelector("[data-refresh]");
    refresh.addEventListener("click", () => busy(refresh, () => send("POST", `${url}/refresh`)));
    const del = row.querySelector("[data-delete]");
    del.addEventListener("click", () => {
        if (!confirm("이 난이도표를 삭제할까요? 클라이언트에서 이 표를 고른 사용자는 다음 동기화부터 받지 않습니다.")) return;
        busy(del, () => send("DELETE", url));
    });
});
