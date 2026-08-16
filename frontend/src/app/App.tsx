import { useMemo } from 'react';
import AdminPage from './AdminPage';
import BookReaderShell from './BookReaderShell';
import ChatPage from './ChatPage';
import HomePage from './HomePage';

const chatRoutePattern = /^\/book\/([1-9]\d*)\/ask\/([A-Za-z0-9]{32,48})\/?$/;

function isHomeRoute(pathname: string): boolean {
  const path = pathname.replace(/^\/+/, '');
  if (!path || path === 'book' || path === 'book/') return true;
  if (path.startsWith('book/api')) return true;
  // Admin route
  if (path.startsWith('admin')) return false;
  // Only canonical numeric paths resolve to a reader.
  return !/^book\/[1-9]\d*\/?$/.test(path);
}

function isAdminRoute(pathname: string): boolean {
  return pathname.replace(/^\/+/, '').startsWith('admin');
}

export default function App() {
  const pathname = useMemo(() => window.location.pathname, []);
  const chatRoute = pathname.match(chatRoutePattern);

  if (isAdminRoute(pathname)) return <AdminPage />;
  if (chatRoute) return <ChatPage bookId={Number(chatRoute[1])} conversationId={chatRoute[2]} />;
  if (isHomeRoute(pathname)) return <HomePage />;
  return <BookReaderShell />;
}
