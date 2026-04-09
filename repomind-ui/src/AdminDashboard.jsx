import { useState, useEffect } from 'react'

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

export default function AdminDashboard() {
  const [queries, setQueries] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [adminUser, setAdminUser] = useState('')
  const [adminPass, setAdminPass] = useState('')
  const [isLoggedIn, setIsLoggedIn] = useState(false)

  async function loadQueries() {
    try {
      const res = await fetch(
        `${API_URL}/api/admin/queries?admin_user=${encodeURIComponent(adminUser)}&admin_pass=${encodeURIComponent(adminPass)}`,
        { 
          method: 'GET',
          headers: { 'Content-Type': 'application/json' }
        }
      )
      
      if (!res.ok) {
        if (res.status === 401) {
          setError('Invalid admin credentials')
        } else {
          setError(`Error: ${res.status}`)
        }
        setLoading(false)
        return
      }
      
      const data = await res.json()
      setQueries(data)
      setIsLoggedIn(true)
    } catch (e) {
      console.error('Fetch error:', e)
      setError('Network error: ' + e.message)
    }
    setLoading(false)
  }

  async function handleLogin(e) {
    e.preventDefault()
    setError('')
    setLoading(true)
    localStorage.setItem('admin_user', adminUser)
    localStorage.setItem('admin_pass', adminPass)
    await loadQueries()
  }

  async function clearQueries() {
    if (!confirm('Clear all out-of-scope queries?')) return

    try {
      const res = await fetch(
        `${API_URL}/api/admin/queries?admin_user=${encodeURIComponent(adminUser)}&admin_pass=${encodeURIComponent(adminPass)}`,
        { method: 'DELETE' }
      )
      if (res.ok) {
        setQueries([])
      }
    } catch (e) {
      alert('Failed to clear queries')
    }
  }

  function handleLogout() {
    localStorage.removeItem('admin_user')
    localStorage.removeItem('admin_pass')
    setAdminUser('')
    setAdminPass('')
    setIsLoggedIn(false)
    setQueries([])
  }

  useEffect(() => {
    const savedUser = localStorage.getItem('admin_user')
    const savedPass = localStorage.getItem('admin_pass')
    if (savedUser && savedPass) {
      setAdminUser(savedUser)
      setAdminPass(savedPass)
      loadQueries()
    } else {
      setLoading(false)
    }
  }, [])

  if (loading) {
    return <div className="admin-loading">Loading admin dashboard...</div>
  }

  if (!isLoggedIn) {
    return (
      <div className="admin-login">
        <h2>Admin Login</h2>
        {error && <p className="auth-error">{error}</p>}
        <form onSubmit={handleLogin}>
          <input
            type="text"
            placeholder="Admin Username"
            value={adminUser}
            onChange={e => setAdminUser(e.target.value)}
            required
          />
          <input
            type="password"
            placeholder="Admin Password"
            value={adminPass}
            onChange={e => setAdminPass(e.target.value)}
            required
          />
          <button type="submit" className="btn btn--primary">Login</button>
        </form>
      </div>
    )
  }

  const totalQueries = queries.reduce((sum, q) => sum + q.count, 0)

  return (
    <div className="admin-dashboard">
      <div className="admin-header">
        <h1>Admin Dashboard</h1>
        <button className="btn btn--outline" onClick={handleLogout}>Logout</button>
      </div>
      
      <div className="admin-stats">
        <div className="stat-card">
          <span className="stat-number">{queries.length}</span>
          <span className="stat-label">Unique Queries</span>
        </div>
        <div className="stat-card">
          <span className="stat-number">{totalQueries}</span>
          <span className="stat-label">Total Occurrences</span>
        </div>
      </div>

      <div className="admin-section">
        <div className="section-header">
          <h2>Out of Scope Queries</h2>
          <button className="btn btn--danger" onClick={clearQueries}>
            Clear All
          </button>
        </div>

        {queries.length === 0 ? (
          <p className="empty-message">No out-of-scope queries yet</p>
        ) : (
          <table className="queries-table">
            <thead>
              <tr>
                <th>Query</th>
                <th>Count</th>
                <th>First Seen</th>
                <th>Last Seen</th>
              </tr>
            </thead>
            <tbody>
              {queries.map((q, i) => (
                <tr key={i}>
                  <td className="query-text">{q.query}</td>
                  <td className="query-count">{q.count}</td>
                  <td>{new Date(q.first_seen).toLocaleDateString()}</td>
                  <td>{new Date(q.last_seen).toLocaleDateString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
