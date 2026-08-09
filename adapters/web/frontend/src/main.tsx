// This entry file is added by the React Vite skeleton to mount the Clonoth web app.
// It imports the global Duties theme once and renders App inside React StrictMode for development checks.
// The purpose is to keep startup minimal while the real Supervisor connection is deferred.
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import App from './App';
import './styles/index.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
