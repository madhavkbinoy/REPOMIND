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