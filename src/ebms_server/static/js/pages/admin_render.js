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

const run = document.getElementById("run");
run.addEventListener("click", () => busy(run, () => send("POST", "/api/admin/render/index/run", {})));

document.querySelectorAll("tr[data-id] [data-status]").forEach((button) => {
    const id = button.closest("tr").dataset.id;
    button.addEventListener("click", () => busy(button, () => send("PUT", `/api/admin/render/findings/${id}`, { status: button.dataset.status })));
});
