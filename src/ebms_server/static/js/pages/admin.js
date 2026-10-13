const GB = 1073741824;
document.querySelectorAll("[data-time]").forEach((el) => {
    el.textContent = new Date(Number(el.dataset.time) * 1000).toLocaleTimeString();
});
document.querySelectorAll("[data-date]").forEach((el) => {
    el.textContent = new Date(Number(el.dataset.date) * 1000).toLocaleString();
});

function values(form, emptyAsNull) {
    const out = {};
    form.querySelectorAll("input[type=number]").forEach((input) => {
        if (input.value === "") {
            if (emptyAsNull) out[input.name] = null;
            return;
        }
        const n = Number(input.value);
        out[input.name] = input.hasAttribute("data-gb") ? Math.round(n * GB) : n;
    });
    return out;
}

async function send(method, url, body) {
    const res = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        alert(data.detail || `실패했습니다 (${res.status})`);
        return false;
    }
    return true;
}

document.getElementById("settings").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (await send("PUT", "/api/admin/settings", values(e.target, false))) location.reload();
});

document.querySelectorAll("form.user").forEach((form) => {
    const url = `/api/admin/users/${form.dataset.id}`;
    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        if (await send("PUT", url, values(form, true))) location.reload();
    });
    form.querySelector("[data-refill]").addEventListener("click", async () => {
        if (await send("POST", `${url}/refill`)) location.reload();
    });
    const status = form.querySelector("[data-status]");
    if (status) {
        status.addEventListener("click", async () => {
            const body = { status: status.dataset.status };
            if (body.status !== "active") {
                const days = prompt("정지할 일수를 입력하세요. 비우면 무기한입니다.\n정지하는 동안 로그인과 모든 API를 쓸 수 없습니다.", "");
                if (days === null) return;
                if (days.trim() !== "") {
                    const n = Number(days);
                    if (!(n > 0)) {
                        alert("일수는 0보다 큰 숫자로 입력하세요.");
                        return;
                    }
                    body.banned_until = Math.floor(Date.now() / 1000 + n * 86400);
                }
            }
            if (await send("PUT", url, body)) location.reload();
        });
    }
});
