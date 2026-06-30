// This Tailwind config is kept even though Tailwind CSS v4 can work mostly from CSS.
// It records why these tokens exist: the web shell follows the Duties cream theme requested for Clonoth.
// The config exposes the same colors and fonts to utility classes so later screens can reuse them consistently.
const config = {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        duties: {
          bg: '#f1f0ee',
          panel: '#e8e7e5',
          muted: '#dddcda',
          text: '#252525',
          secondary: '#4a4a4a',
          tertiary: '#6e6e73',
          border: '#ccc',
          accent: '#feaf2c',
        },
      },
      fontFamily: {
        mono: ['Geist Mono', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
      },
      borderRadius: {
        none: '0',
        minimal: '2px',
      },
    },
  },
};

export default config;
