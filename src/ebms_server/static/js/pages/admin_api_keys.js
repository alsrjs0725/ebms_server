document.querySelectorAll("[data-date]").forEach((el) => {
    el.textContent = new Date(Number(el.dataset.date) * 1000).toLocaleString();
});

async function fail(res) {
    const data = await res.json().catch(() => ({}));
    alert(data.detail || `실패했습니다 (${res.status})`);
}

document.getElementById("create").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const res = await fetch("/api/admin/api-keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: form.name.value, days: Number(form.days.value) }),
    });
    if (!res.ok) return fail(res);
    const data = await res.json();
    // 원문은 다시 볼 수 없으므로 새로고침하지 않고 바로 보여 줍니다.
    document.getElementById("created-key").textContent = data.key;
    document.getElementById("created").classList.remove("d-none");
    form.name.value = "";
});

document.querySelectorAll("[data-revoke]").forEach((button) => {
    button.addEventListener("click", async () => {
        if (!confirm("이 키를 폐기할까요? 이 키로 하던 호출은 바로 막힙니다.")) return;
        const res = await fetch(`/api/admin/api-keys/${button.dataset.revoke}`, { method: "DELETE" });
        if (!res.ok) return fail(res);
        button.closest("tr").remove();
    });
});
