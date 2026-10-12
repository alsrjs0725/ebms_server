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

document.querySelectorAll("form.policy-form").forEach((form) => {
    const kind = form.dataset.kind;
    const body = form.querySelector("[name=body]");
    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const effective_on = form.querySelector("[name=effective_on]").value || null;
        if (await send("POST", `/api/admin/policies/${kind}`, { body: body.value, effective_on })) location.reload();
    });
    form.querySelector("[data-template]").addEventListener("click", async () => {
        if (!confirm("편집 중인 내용을 기본 템플릿으로 바꿀까요? 저장하기 전에는 공개 페이지가 바뀌지 않습니다.")) return;
        const data = await send("GET", `/api/admin/policies/${kind}/template`);
        if (data) body.value = data.body;
    });
});

document.querySelectorAll("[data-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
        if (!confirm("이 판을 삭제할까요? 변경 이력에서도 사라집니다.")) return;
        if (await send("DELETE", `/api/admin/policies/versions/${button.dataset.delete}`)) location.reload();
    });
});
