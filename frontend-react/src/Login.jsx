import { useState } from 'react';
import { GoogleAuthProvider, signInWithPopup, getIdToken } from 'firebase/auth';
import { getFirebaseAuth } from './firebase.js';

const T = {
  void:      '#090c12',
  base:      '#0e1220',
  surface:   '#141826',
  surfaceHi: '#1a2030',
  surfaceEl: '#1f263a',
  border:    '#252d42',
  borderHi:  '#303a54',
  amber:     '#e8a442',
  amberDim:  '#a06c22',
  live:      '#39e07a',
  danger:    '#e05555',
  textPri:   '#dde4f0',
  textSec:   '#7a8aaa',
  textDim:   '#3d4860',
};

function GoogleIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
      <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4"/>
      <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
      <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z" fill="#FBBC05"/>
      <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
    </svg>
  );
}

export default function Login({ onLogin }) {
  const [status, setStatus] = useState('idle'); // idle | loading | error
  const [error, setError] = useState(null);

  const handleGoogleSignIn = async () => {
    setStatus('loading');
    setError(null);
    try {
      const auth = getFirebaseAuth();
      const provider = new GoogleAuthProvider();
      const result = await signInWithPopup(auth, provider);
      const idToken = await getIdToken(result.user);

      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ idToken }),
      });
      if (!res.ok) throw new Error('Backend rejected the login — check that the server is running.');

      onLogin(result.user, idToken);
    } catch (e) {
      if (e.code === 'auth/popup-closed-by-user') {
        setStatus('idle');
      } else {
        setStatus('error');
        setError(e.message);
      }
    }
  };

  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      height: '100vh', background: T.void,
      fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    }}>
      <div style={{
        width: 340,
        background: T.surface,
        border: `1px solid ${T.border}`,
        borderTop: `2px solid ${T.amber}`,
        borderRadius: 8,
        overflow: 'hidden',
      }}>

        {/* Header */}
        <div style={{ padding: '32px 28px 24px', borderBottom: `1px solid ${T.border}` }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 6 }}>
            <span style={{
              fontSize: 22, fontWeight: 800, letterSpacing: '1.2px',
              color: T.amber, textTransform: 'uppercase',
            }}>
              FieldRaven
            </span>
            <span style={{
              fontSize: 10, color: T.textDim, letterSpacing: '.6px',
              textTransform: 'uppercase', fontWeight: 600,
            }}>
              desktop
            </span>
          </div>
          <p style={{ fontSize: 12, color: T.textSec, lineHeight: 1.5, margin: 0 }}>
            Sign in to register this machine and access your field jobs.
          </p>
        </div>

        {/* Sign-in */}
        <div style={{ padding: '24px 28px' }}>
          <button
            onClick={status === 'loading' ? undefined : handleGoogleSignIn}
            disabled={status === 'loading'}
            style={{
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              gap: 10, width: '100%', padding: '10px 16px',
              background: status === 'loading' ? T.surfaceEl : T.surfaceHi,
              border: `1px solid ${T.borderHi}`,
              borderRadius: 5, cursor: status === 'loading' ? 'not-allowed' : 'pointer',
              color: T.textPri, fontSize: 13, fontWeight: 600,
              fontFamily: 'inherit', transition: 'all .15s',
              opacity: status === 'loading' ? 0.6 : 1,
            }}
          >
            {status === 'loading' ? (
              <>
                <span style={{
                  width: 14, height: 14, borderRadius: '50%',
                  border: `2px solid ${T.textDim}`,
                  borderTopColor: T.amber,
                  display: 'inline-block',
                  animation: 'spin .7s linear infinite',
                }} />
                Signing in…
              </>
            ) : (
              <>
                <GoogleIcon />
                Continue with Google
              </>
            )}
          </button>

          {status === 'error' && error && (
            <div style={{
              marginTop: 12, padding: '8px 10px',
              background: `${T.danger}18`, border: `1px solid ${T.danger}44`,
              borderRadius: 4, fontSize: 11, color: T.danger, lineHeight: 1.5,
            }}>
              {error}
            </div>
          )}
        </div>

        {/* Footer */}
        <div style={{
          padding: '10px 28px 16px',
          fontSize: 10, color: T.textDim, textAlign: 'center',
        }}>
          Session persists — you won't be asked again for 30+ days.
        </div>
      </div>

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
