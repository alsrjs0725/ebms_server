document.querySelectorAll("[data-time]").forEach((el) => {
    el.textContent = new Date(Number(el.dataset.time) * 1000).toLocaleString();
});
document.querySelectorAll("[data-clock]").forEach((el) => {
    el.textContent = new Date(Number(el.dataset.clock) * 1000).toLocaleTimeString();
});
document.querySelectorAll("[data-delete]").forEach((btn) => {
    btn.addEventListener("click", async () => {
        if (!confirm(btn.dataset.confirm)) return;
        const res = await fetch(btn.dataset.delete, { method: "DELETE" });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            alert(body.detail || `실패했습니다 (${res.status})`);
        }
        location.reload();
    });
});
