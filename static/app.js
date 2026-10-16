const menuToggle = document.getElementById("menu-toggle");
const sidebar = document.getElementById("sidebar");
const mobileScrim = document.getElementById("mobile-scrim");

if (menuToggle && sidebar && mobileScrim) {
function setMenuOpen(isOpen) {
    sidebar.classList.toggle("sidebar-open", isOpen);
    mobileScrim.classList.toggle("scrim-visible", isOpen);
    menuToggle.setAttribute("aria-expanded", String(isOpen));
}

menuToggle.addEventListener("click", () => {
    setMenuOpen(!sidebar.classList.contains("sidebar-open"));
});

mobileScrim.addEventListener("click", () => setMenuOpen(false));
sidebar.querySelectorAll(".nav-link").forEach((link) => {
    link.addEventListener("click", () => setMenuOpen(false));
});
}
