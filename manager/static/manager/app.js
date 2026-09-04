document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector("[data-nav-toggle]");
  const nav = document.querySelector("[data-nav]");
  if (toggle && nav) toggle.addEventListener("click", () => nav.classList.toggle("open"));
  document.querySelectorAll("form[data-confirm]").forEach((form) => form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  }));
  const refresh = document.querySelector("[data-auto-refresh]");
  if (refresh) window.setTimeout(() => window.location.reload(), Number(refresh.dataset.autoRefresh || 3000));
});
