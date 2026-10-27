document.addEventListener("DOMContentLoaded", () => {
    const mode = document.getElementById("search-mode");
    if (!mode) return;
    const formula = document.getElementById("formula");
    const mass = document.getElementById("query-mass");
    function update() {
        const byMass = mode.value === "mass";
        document.getElementById("formula-fields").hidden = byMass;
        document.getElementById("mass-fields").hidden = !byMass;
        formula.disabled = byMass;
        formula.required = !byMass;
        mass.required = byMass;
        for (const control of document.querySelectorAll("#mass-fields input, #mass-fields select")) {
            control.disabled = !byMass;
        }
    }
    mode.addEventListener("change", update);
    update();
});
