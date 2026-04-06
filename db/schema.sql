CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY, repo TEXT NOT NULL, source_type TEXT NOT NULL,
  number INTEGER, file_path TEXT, chunk_index INTEGER DEFAULT 0,
  url TEXT, title TEXT
);

CREATE TABLE IF NOT EXISTS bm25_index (
  chunk_id TEXT PRIMARY KEY, text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS code_issue_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL,
  file_path TEXT NOT NULL, pr_number INTEGER, issue_number INTEGER,
  link_type TEXT
);

CREATE INDEX IF NOT EXISTS idx_links_file  ON code_issue_links(file_path);
CREATE INDEX IF NOT EXISTS idx_links_issue ON code_issue_links(issue_number);
CREATE INDEX IF NOT EXISTS idx_links_pr    ON code_issue_links(pr_number);

-- User authentication
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  is_admin INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Chat history persistence
CREATE TABLE IF NOT EXISTS chat_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_history(user_id);

-- User sessions
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token TEXT UNIQUE NOT NULL,
  user_id INTEGER NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Out of scope queries tracking
CREATE TABLE IF NOT EXISTS out_of_scope_queries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query TEXT NOT NULL,
  count INTEGER DEFAULT 1,
  first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);