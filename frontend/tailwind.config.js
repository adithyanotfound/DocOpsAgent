/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Poppins", "sans-serif"]
      },
      colors: {
        ink: "#000000",
        paper: "#FFFFFF",
        accent: "#FFD600"
      }
    }
  },
  plugins: []
};
