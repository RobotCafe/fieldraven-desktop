import { useState, useEffect } from 'react';
import { onAuthStateChanged, getIdToken } from 'firebase/auth';
import { initFirebase, getFirebaseAuth } from './firebase.js';
import Login from './Login.jsx';
import FieldRavenDesktop from './App.jsx';

const T = {
  void: '#090c12',
  amber: '#e8a442',
  textSec: '#7a8aaa',
};

function BootScreen({ message, error }) {
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', alignItems: 'center',
      justifyContent: 'center', height: '100vh', background: T.void, gap: 16,
      fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    }}>
      {!error && (
        <span style={{
          width: 20, height: 20, borderRadius: '50%',
          border: '2px solid #252d42', borderTopColor: T.amber,
          display: 'inline-block', animation: 'spin .7s linear infinite',
        }} />
      )}
      <span style={{ fontSize: 12, color: error ? '#e05555' : T.textSec }}>
        {message}
      </span>
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}

async function registerMachine(idToken) {
  try {
    await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ idToken }),
    });
  } catch {
    // Non-fatal — machine registration is best-effort on session restore
  }
}

export default function Root() {
  // 'booting' | 'login' | 'app' | 'error'
  const [phase, setPhase] = useState('booting');
  const [bootError, setBootError] = useState(null);
  const [currentUser, setCurrentUser] = useState(null);
  const [idToken, setIdToken] = useState(null);

  useEffect(() => {
    let unsubscribe;

    initFirebase()
      .then(auth => {
        unsubscribe = onAuthStateChanged(auth, async (user) => {
          if (user) {
            const token = await getIdToken(user);
            setCurrentUser(user);
            setIdToken(token);
            await registerMachine(token);
            setPhase('app');
          } else {
            setPhase('login');
          }
        });
      })
      .catch(err => {
        setBootError(err.message);
        setPhase('error');
      });

    return () => unsubscribe?.();
  }, []);

  const handleLogin = async (user, token) => {
    setCurrentUser(user);
    setIdToken(token);
    await registerMachine(token);
    setPhase('app');
  };

  const handleSignOut = () => {
    const auth = getFirebaseAuth();
    auth?.signOut();
    setCurrentUser(null);
    setIdToken(null);
    setPhase('login');
  };

  if (phase === 'booting') return <BootScreen message="Connecting to FieldRaven…" />;
  if (phase === 'error')   return <BootScreen message={bootError} error />;
  if (phase === 'login')   return <Login onLogin={handleLogin} />;

  return <FieldRavenDesktop user={currentUser} idToken={idToken} onSignOut={handleSignOut} />;
}
