/* Masks a secret's value field until the user asks to see it.
 *
 * The textarea is rendered masked (data-masked="1", -webkit-text-security);
 * a browser without that property gets a blur instead. A button with
 * data-secret-toggle="<field id>" flips it, with no reload. Typing works the
 * same either way; the value is submitted unchanged.
 */
(function () {
    var supported = window.CSS && CSS.supports && CSS.supports("-webkit-text-security", "disc");

    function apply(field, masked) {
        field.dataset.masked = masked ? "1" : "0";
        if (supported) {
            field.style.webkitTextSecurity = masked ? "disc" : "none";
        } else {
            field.style.filter = masked ? "blur(4px)" : "none";
        }
    }

    document.querySelectorAll("[data-secret-toggle]").forEach(function (button) {
        var field = document.getElementById(button.dataset.secretToggle);
        if (!field) return;
        apply(field, true);
        button.setAttribute("aria-controls", field.id);
        button.setAttribute("aria-pressed", "false");
        button.addEventListener("click", function () {
            var show = field.dataset.masked === "1";
            apply(field, !show);
            button.textContent = show ? "Hide" : "Show";
            button.setAttribute("aria-pressed", show ? "true" : "false");
        });
    });
})();
