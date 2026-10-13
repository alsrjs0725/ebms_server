document.querySelectorAll("[data-time]").forEach((el) => {
    el.textContent = new Date(Number(el.dataset.time) * 1000).toLocaleString();
});

async function send(method, url, body) {
    const res = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("\n") : data.detail;
        alert(detail || `실패했습니다 (${res.status})`);
        return null;
    }
    return data;
}

const form = document.getElementById("home-form");
const body = form.querySelector("[name=body]");
form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (await send("PUT", "/api/admin/home", { body: body.value })) location.reload();
});
form.querySelector("[data-default]").addEventListener("click", async () => {
    if (!confirm("편집 중인 내용을 기본 글로 바꿀까요? 저장하기 전에는 메인화면이 바뀌지 않습니다.")) return;
    const data = await send("GET", "/api/admin/home");
    if (data) body.value = data.default;
});
