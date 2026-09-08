/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["'IBM Plex Sans'", "sans-serif"],
        display: ["'Space Grotesk'", "sans-serif"],
        mono: ["'JetBrains Mono'", "monospace"],
      },
      boxShadow: {
        glow: "0 0 0 1px rgba(255,255,255,0.05), 0 24px 80px rgba(0,0,0,0.35)",
      },
      colors: {
        // 统一设计系统（对标 Hyperliquid 交易页）：
        // cyan 语义 = 平台强调色（绿青），slate-900/950 = 平台面板/背景层级。
        // 旧页面沿用 cyan-*/slate-900 类名即可自动获得统一风格。
        cyan: {
          50: "#e8faf4",
          100: "#c9f2e6",
          200: "#93e6cd",
          300: "#5ed6b0",
          400: "#38cba0",
          500: "#26b98d",
          600: "#1a9a76",
          700: "#177a5f",
          800: "#14604c",
          900: "#11473a",
          950: "#0a2f27",
        },
        slate: {
          50: "#f4f8f7",
          100: "#e2ece9",
          200: "#c6d8d4",
          300: "#a3bcb8",
          400: "#7e9490",
          500: "#5f7773",
          600: "#4a5f5b",
          700: "#3b4d4a",
          800: "#283836",
          900: "#0e1a1b",
          950: "#0b1517",
        },
      },
    },
  },
  plugins: [],
};
