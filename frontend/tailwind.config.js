/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx,ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: '#0d1117',
        surface: '#161b22',
        border: '#30363d',
      },
    },
  },
  plugins: [],
  // 停用 preflight，避免與 Ant Design 的 CSS normalize 衝突
  corePlugins: {
    preflight: false,
  },
}
