import { initializeApp } from 'firebase/app';
import { getAuth } from 'firebase/auth';

let _auth = null;

export async function initFirebase() {
  const res = await fetch('/api/firebase-config');
  if (!res.ok) throw new Error(`Could not load Firebase config from backend (${res.status}). Is the server running?`);
  const config = await res.json();
  const app = initializeApp(config);
  _auth = getAuth(app);
  return _auth;
}

export function getFirebaseAuth() {
  return _auth;
}
