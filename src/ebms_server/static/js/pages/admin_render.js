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

// 차트 렌더 음원 듣기: 서버가 만들 때까지 상태를 묻고, 다 되면 아래 재생기로 엽니다.
const player = document.getElementById("player");
const playerLabel = document.getElementById("player-label");
const playerAudio = document.getElementById("player-audio");
let listenToken = 0;

async function listen(button) {
    const token = ++listenToken;
    const { song, chart } = button.dataset;
    const q = new URLSearchParams({ song, chart });
    const name = `#${song} ${chart}`;
    player.classList.remove("d-none");
    playerAudio.pause();
    playerAudio.removeAttribute("src");
    playerLabel.textContent = `${name}: 렌더 중…`;
    for (;;) {
        const res = await fetch(`/api/admin/render/audio?${q}`);
        if (token !== listenToken) return;
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.status === "error") {
            playerLabel.textContent = `${name}: ${data.error || data.detail || `실패했습니다 (${res.status})`}`;
            return;
        }
        if (data.status === "ready") break;
        await new Promise((r) => setTimeout(r, 1500));
        if (token !== listenToken) return;
    }
    playerLabel.textContent = name;
    playerAudio.src = `/api/admin/render/audio/file?${q}`;
    playerAudio.play().catch(() => {});
}

document.querySelectorAll("[data-listen]").forEach((button) => {
    button.addEventListener("click", () => listen(button));
});
