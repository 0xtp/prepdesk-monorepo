/** Tailwind is used only at build time; the compiled CSS is committed (no Node in production). */
module.exports = {
  content: ["../src/perpdesk/web/templates/**/*.html"],
  theme: {
    extend: {
      fontFamily: { sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"] },
    },
  },
  plugins: [require("@tailwindcss/forms")],
};
