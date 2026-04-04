import { useState, useRef, useEffect } from 'react'
import './App.css'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'
const REPO    = 'kubernetes/kubernetes'

export default function App() {
  const [messages, setMessages] = useState([])
  const [input,    setInput   ] = useState('')
  const [loading,  setLoading ] = useState(false)
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  async function send() {
    if (!input.trim() || loading) return
    const question = input.trim()
    setInput('')
    setLoading(true)

    const history = messages.map(m => ({ role: m.role, content: m.content }))
    setMessages(prev => [...prev, { role: 'user', content: question }])

    const res = await fetch(`${API_URL}/api/chat`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ question, repo: REPO, history }),
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

  return (
    <div className="app">
      <header className="header">
        <h1>RepoMind</h1>
        <span className="repo-badge">{REPO}</span>
      </header>

      <div className="messages">
        {messages.length === 0 && (
          <div className="empty-state">
            <p>Ask anything about design decisions in <strong>{REPO}</strong></p>
            <p className="hint">e.g. "Why did Kubernetes deprecate Dockershim?"</p>
          </div>
        )}

        {messages.map((m, i) => (
          <div key={i} className={`message message--${m.role}`}>
            <div className="message__bubble">
              <pre className="message__text">{m.content}</pre>

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