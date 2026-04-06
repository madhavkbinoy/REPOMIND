import { useState, useRef, useEffect } from 'react'
import './App.css'
import AdminDashboard from './AdminDashboard'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'
const REPO    = 'kubernetes/kubernetes'

export default function App() {
  const [messages, setMessages] = useState([])
  const [input,    setInput   ] = useState('')
  const [loading,  setLoading ] = useState(false)
  const bottomRef = useRef(null)

  // Auth state
  const [user, setUser] = useState(null)
  const [showAuth, setShowAuth] = useState(false)
  const [authMode, setAuthMode] = useState('login')
  const [authUsername, setAuthUsername] = useState('')
  const [authPassword, setAuthPassword] = useState('')
  const [authError, setAuthError] = useState('')
  const [showAdmin, setShowAdmin] = useState(false)

  // Check for existing session on load
  useEffect(() => {
    const token = localStorage.getItem('token')
    if (token) {
      fetch(`${API_URL}/api/me?token=${token}`)
        .then(r => r.json())
        .then(data => {
          if (data.authenticated) {
            setUser(data)
            loadChatHistory(data.user_id)
          }
        })
        .catch(() => localStorage.removeItem('token'))
    }
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function loadChatHistory(userId) {
    try {
      const res = await fetch(`${API_URL}/api/history?user_id=${userId}`)
      const history = await res.json()
      if (history.length > 0) {
        setMessages(history.map(m => ({ role: m.role, content: m.content })))
      }
    } catch (e) {
      console.error('Failed to load history:', e)
    }
  }

  async function handleAuth(e) {
    e.preventDefault()
    setAuthError('')
    
    const endpoint = authMode === 'login' ? '/login' : '/register'
    try {
      const res = await fetch(`${API_URL}/api${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: authUsername, password: authPassword })
      })
      
      const data = await res.json()
      
      if (!res.ok) {
        setAuthError(data.detail || 'Authentication failed')
        return
      }
      
      localStorage.setItem('token', data.token)
      setUser(data)
      setShowAuth(false)
      setAuthUsername('')
      setAuthPassword('')
      
      if (authMode === 'register') {
        setMessages([])
      } else {
        loadChatHistory(data.user_id)
      }
    } catch (e) {
      setAuthError('Network error')
    }
  }

  function handleLogout() {
    localStorage.removeItem('token')
    setUser(null)
    setMessages([])
    setShowAdmin(false)
  }

  async function clearChat() {
    if (user) {
      await fetch(`${API_URL}/api/history?user_id=${user.user_id}`, { method: 'DELETE' })
    }
    setMessages([])
  }

  async function send() {
    if (!input.trim() || loading) return
    const question = input.trim()
    setInput('')
    setLoading(true)

    const history = messages.map(m => ({ role: m.role, content: m.content }))
    setMessages(prev => [...prev, { role: 'user', content: question }])

    const payload = { 
      question, 
      repo: REPO, 
      history,
      user_id: user?.user_id || null
    }

    const res = await fetch(`${API_URL}/api/chat`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload),
    })

    const contentType = res.headers.get('content-type') || ''
    if (!contentType.includes('text/event-stream')) {
      const data = await res.json()
      setMessages(prev => [...prev, {
        role:             'assistant',
        content:          data.answer,
        sources:          data.sources || [],
        is_fallback:      data.is_fallback,
        citations_valid:  data.citations_valid,
        invalid_citations: data.invalid_citations || [],
      }])
      setLoading(false)
      return
    }

    const reader = res.body.getReader()
    const dec    = new TextDecoder()
    let   buf    = ''
    let   answer = ''

    setMessages(prev => [...prev, { role: 'assistant', content: '' }])

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      const parts = buf.split('\n\n')
      buf = parts.pop()

      for (const part of parts) {
        if (!part.startsWith('data: ')) continue
        let data
        try { data = JSON.parse(part.slice(6)) } catch { continue }

        if (data.token) {
          answer += data.token
          setMessages(prev => [
            ...prev.slice(0, -1),
            { role: 'assistant', content: answer },
          ])
        }
        if (data.done) {
          setMessages(prev => [
            ...prev.slice(0, -1),
            {
              role:             'assistant',
              content:          answer,
              sources:          data.sources || [],
              is_fallback:      data.is_fallback,
              citations_valid:  data.citations_valid,
              invalid_citations: data.invalid_citations || [],
            },
          ])
        }
      }
    }
    setLoading(false)
  }

  function handleKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  function renderContent(content) {
    const parts = content.split(/(\(#\d+\))/g)
    return parts.map((part, i) => {
      if (part.match(/\(#\d+\)/)) {
        return <span key={i} className="citation">{part}</span>
      }
      return part
    })
  }

  return (
    <div className="app">
      <header className="header">
        <h1>RepoMind</h1>
        <div className="header__right">
          <span className="repo-badge">{REPO}</span>
          <button className="btn btn--outline" onClick={() => setShowAdmin(true)}>Admin</button>
          {user ? (
            <div className="user-menu">
              <span className="username">{user.username}</span>
              <button className="btn btn--outline" onClick={clearChat}>Clear Chat</button>
              <button className="btn btn--outline" onClick={handleLogout}>Logout</button>
            </div>
          ) : (
            <button className="btn btn--primary" onClick={() => setShowAuth(true)}>Login</button>
          )}
        </div>
      </header>

      {showAuth && (
        <div className="auth-modal">
          <div className="auth-box">
            <h2>{authMode === 'login' ? 'Login' : 'Register'}</h2>
            {authError && <p className="auth-error">{authError}</p>}
            <form onSubmit={handleAuth}>
              <input
                type="text"
                placeholder="Username"
                value={authUsername}
                onChange={e => setAuthUsername(e.target.value)}
                required
              />
              <input
                type="password"
                placeholder="Password"
                value={authPassword}
                onChange={e => setAuthPassword(e.target.value)}
                required
              />
              <button type="submit" className="btn btn--primary">
                {authMode === 'login' ? 'Login' : 'Register'}
              </button>
            </form>
            <p className="auth-switch">
              {authMode === 'login' ? "Don't have an account?" : "Already have an account?"}
              <button onClick={() => setAuthMode(authMode === 'login' ? 'register' : 'login')}>
                {authMode === 'login' ? 'Register' : 'Login'}
              </button>
            </p>
            <button className="auth-close" onClick={() => setShowAuth(false)}>×</button>
          </div>
        </div>
      )}

      {showAdmin && (
        <div className="auth-modal" style={{ display: 'flex', position: 'fixed', zIndex: 9999 }}>
          <div className="admin-modal" style={{ background: 'white', padding: '20px', maxWidth: '800px', width: '90%', maxHeight: '80vh', overflow: 'auto' }}>
            <button className="auth-close" onClick={() => setShowAdmin(false)}>×</button>
            <AdminDashboard />
          </div>
        </div>
      )}

      <div className="messages">
        {messages.length === 0 && (
          <div className="empty-state">
            <p>Ask anything about design decisions in <strong>{REPO}</strong></p>
            <p className="hint">e.g. "Why did Kubernetes deprecate Dockershim?"</p>
            {!user && <p className="hint">Login to save your chat history</p>}
          </div>
        )}

        {messages.map((m, i) => (
          <div key={i} className={`message message--${m.role}`}>
            <div className="message__bubble">
              <div className="message__text">{renderContent(m.content)}</div>

              {m.is_fallback && (
                <p className="message__fallback">
                  ⚠ No confident answer found in indexed data.
                </p>
              )}

              {m.citations_valid === false && (
                <p className="message__warning">
                  ⚠ Some citations were removed — they did not match the source content.
                  The answer reflects only what the indexed issues directly state.
                </p>
              )}

              {m.sources && m.sources.length > 0 && (
                <div className="message__sources">
                  <p className="sources__label">Sources</p>
                  {m.sources.map((s, j) => (
                    <a
                      key={j}
                      href={s.url}
                      target="_blank"
                      rel="noreferrer"
                      className="sources__link"
                    >
                      {s.source_type} #{s.number}: {s.title}
                    </a>
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}

        {loading && (
          <div className="message message--assistant">
            <div className="message__bubble">
              <span className="typing">●●●</span>
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      <div className="input-row">
        <textarea
          className="input-box"
          rows={2}
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKey}
          placeholder="Ask about a design decision… (Enter to send, Shift+Enter for newline)"
          disabled={loading}
        />
        <button
          className="send-btn"
          onClick={send}
          disabled={loading || !input.trim()}
        >
          {loading ? '…' : 'Ask'}
        </button>
      </div>
    </div>
  )
}
