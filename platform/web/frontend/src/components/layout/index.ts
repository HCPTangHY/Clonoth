// This layout barrel is added so App can import the shell components from one module.
// It keeps the folder boundary clear: layout contains structure, while chat contains message-specific UI.
// The purpose is to match the requested directory architecture and reduce import noise.
export { AppLayout } from './AppLayout';
export { Header } from './Header';
export { Sidebar } from './Sidebar';
