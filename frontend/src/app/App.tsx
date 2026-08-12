import { useMemo } from 'react';
import AdminPage from './AdminPage';
import BookReaderShell from './BookReaderShell';
import HomePage from './HomePage';

function isHomeRoute(pathname: string): boolean {
  const path = pathname.replace(/^\/+/, '');
  if (!path || path === 'book' || path === 'book/') return true;
  if (path.startsWith('book/api')) return true;
  // Admin route
  if (path.startsWith('admin')) return false;
  // Anything else under /book/<bookId>/... is a book reader route
  return !path.startsWith('book/');
}

function isAdminRoute(pathname: string): boolean {
  return pathname.replace(/^\/+/, '').startsWith('admin');
}

export default function App() {
  const pathname = useMemo(() => window.location.pathname, []);

  if (isAdminRoute(pathname)) return <AdminPage />;
  if (isHomeRoute(pathname)) return <HomePage />;
  return <BookReaderShell />;
}
