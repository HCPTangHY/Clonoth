// Vite builds the React frontend mounted under /web/.
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  base: '/web/',
  plugins: [react(), tailwindcss()],
});
